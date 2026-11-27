"""FastAPI-приложение: внутренний REST API + static + /admin/token.

Frontend никогда не получает bearer-токены и не обращается к True API.
"""
from __future__ import annotations

import base64
import hmac
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from admin_auth import authorized, install_admin_sessions, valid_key
from cis_check import install_cis_check
from config import settings
from models import AppError, status_ru
from organizations import registry
from buildinfo import get_build_id
from schemas import (
    BalanceRequest,
    ScanBatchRequest,
    ScanRequest,
    TokenUpdateRequest,
)
from service import Service, build_service
from km_split_client import KmSplitClient

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="GIS MT True API — internal tool")

svc: Service = build_service()
km_split_client = KmSplitClient(settings.km_split_url, settings.km_split_internal_token)
svc.km_split_client = km_split_client
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class SignerResult(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    lease: str = Field(min_length=1, max_length=128)
    sha256: str = Field(min_length=64, max_length=64)
    signature: str = Field(min_length=1, max_length=1024 * 1024)
    thumbprint: str = Field(min_length=40, max_length=64)


class SignerHeartbeat(BaseModel):
    thumbprint: str = Field(min_length=40, max_length=64)
    expires: str = Field(min_length=1, max_length=80)
    status: str = Field(max_length=120)


@app.on_event("startup")
async def startup() -> None:
    pass


@app.on_event("shutdown")
async def shutdown() -> None:
    await svc.aclose()
    await km_split_client.aclose()


def _error_body(e: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": True, "category": e.category, "message": e.message},
    )


# ---- REST API ----------------------------------------------------------

# ---- Shared Windows signer --------------------------------------------

def _require_signer(request: Request):
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not settings.signer_token or not hmac.compare_digest(supplied, settings.signer_token):
        raise HTTPException(403, "Подписант не авторизован")
    if request.url.scheme != "https" and request.client.host not in ("testclient", "127.0.0.1", "::1"):
        raise HTTPException(403, "Требуется HTTPS")


@app.post("/api/signer/heartbeat")
def signer_heartbeat(body: SignerHeartbeat, request: Request):
    _require_signer(request)
    thumbprint = body.thumbprint.replace(" ", "").upper()
    if not re.fullmatch(r"[A-F0-9]{40}", thumbprint) or thumbprint != settings.signer_thumbprint:
        raise HTTPException(403, "Отпечаток сертификата не совпадает с GIS_SIGNER_THUMBPRINT")
    warehouse.signer_heartbeat(thumbprint, body.expires, body.status)
    return {"ok": True}


@app.post("/api/signer/next")
def signer_next(request: Request):
    _require_signer(request)
    if not re.fullmatch(r"[A-F0-9]{40}", settings.signer_thumbprint):
        raise HTTPException(409, "Настройте GIS_SIGNER_THUMBPRINT")
    job = warehouse.signer_next(settings.signer_thumbprint)
    return job if job else Response(status_code=204)


@app.post("/api/signer/result")
async def signer_result(body: SignerResult, request: Request):
    _require_signer(request)
    try:
        signature = base64.b64decode(body.signature, validate=True)
        if not 64 <= len(signature) <= 750000:
            raise ValueError()
    except Exception:
        raise HTTPException(422, "Неверный формат подписи")
    thumbprint = body.thumbprint.replace(" ", "").upper()
    if not hmac.compare_digest(thumbprint, settings.signer_thumbprint):
        raise HTTPException(403, "Другой сертификат")
    try:
        job = warehouse.signer_claim_result(body.id, body.lease, body.sha256)
    except Exception as exc:
        from warehouse_store import WarehouseError
        if isinstance(exc, WarehouseError):
            raise HTTPException(exc.status, exc.message) from exc
        raise
    try:
        await svc.exchange_token(job["inn"], job["challenge_uuid"], body.signature)
    except AppError as exc:
        warehouse.signer_job_finish(body.id, "failed", exc.message)
        return {"id": body.id, "state": "failed", "message": exc.message}
    except Exception:
        warehouse.signer_job_finish(body.id, "failed", "Не удалось подтвердить авторизацию в True API. Запросите новый challenge.")
        return {"id": body.id, "state": "failed",
                "message": "Не удалось подтвердить авторизацию в True API. Запросите новый challenge."}
    warehouse.signer_job_finish(body.id, "done")
    return {"id": body.id, "state": "done", "message": "Токен True API обновлён"}


@app.get("/api/status")
async def api_status():
    orgs = []
    for o in registry.all():
        orgs.append(
            {
                "inn": o.inn,
                "name": o.name,
                "token_configured": svc.tokens.has(o.inn),
                "token_updated_at": svc.tokens.updated_at(o.inn),
            }
        )
    return {
        "status": "ok",
        "mode": settings.mode,
        "backend": "online",
        "build_id": get_build_id(),
        "trueapi": svc.trueapi_state(),
        "organizations": orgs,
        "signer": {"configured": bool(settings.signer_token and settings.signer_thumbprint)},
    }


@app.post("/api/scan")
async def api_scan(req: ScanRequest):
    result = await svc.scan(req.code, req.org_inn)
    return result.as_dict()


@app.post("/api/scan_batch")
async def api_scan_batch(req: ScanBatchRequest):
    results = await svc.scan_batch(req.codes, req.org_inn)
    return {"results": [r.as_dict() for r in results]}


@app.post("/api/balance")
async def api_balance(req: BalanceRequest):
    try:
        out = await svc.balance(req.gtin)
    except AppError as e:
        return _error_body(e)
    return out


# ---- /admin/token ------------------------------------------------------


def _check_admin(key: Optional[str]) -> bool:
    """Fail-closed: без настроенного ADMIN_KEY доступ запрещён.

    Сравнение через hmac.compare_digest (constant-time, не палит длину ключа).
    """
    return valid_key(key, settings.admin_key)


@app.get("/admin/token")
async def admin_token_page():
    return RedirectResponse('/admin?section=token', status_code=307)


@app.post("/admin/token")
async def admin_token_post(
    request: Request,
    inn: str = Form(...),
    uuid: str = Form(...),
    signature: str = Form(...),
):
    if not authorized(request, settings.admin_key):
        return JSONResponse({"ok": False, "message": "Доступ запрещён"}, status_code=403)
    try:
        token = await svc.exchange_token(inn.strip(), uuid.strip(), signature.strip())
    except AppError as e:
        return JSONResponse({"ok": False, "inn": inn, "message": e.message})
    # НЕ возвращаем сам токен обратно в ответ
    return {"ok": True, "inn": inn.strip(), "message": "Токен обновлён"}


# ---- Warehouse documents ------------------------------------------------
from sales_shipments import install_sales_shipments
from warehouse_store import WarehouseStore
from warehouse_routes import install_warehouse

warehouse = WarehouseStore(settings.warehouse_db)
svc.policy_getter = lambda: warehouse.application_settings()['values']
install_admin_sessions(app, lambda: settings.admin_key)
install_warehouse(app, lambda: svc, lambda: warehouse, templates)
install_sales_shipments(app, lambda: svc, lambda: warehouse, templates)
install_cis_check(app, lambda: svc)


@app.get("/admin/participants", response_class=HTMLResponse, include_in_schema=False)
async def participants_page(request: Request):
    if not authorized(request, settings.admin_key):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request=request, name="participants.html",
        context={"build_id": get_build_id(), "mock": settings.is_mock},
        headers={"Cache-Control": "no-store"},
    )


# ---- static ------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"build_id": get_build_id()},
    )


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
async def startup() -> None:
    pass


@app.on_event("shutdown")
async def shutdown() -> None:
    await svc.aclose()


def _error_body(e: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": True, "category": e.category, "message": e.message},
    )


# ---- REST API ----------------------------------------------------------

@app.get("/api/status")
async def api_status():
    orgs = []
    for o in registry.all():
        orgs.append(
            {
                "inn": o.inn,
                "name": o.name,
                "token_configured": svc.tokens.has(o.inn),
                "token_updated_at": svc.tokens.updated_at(o.inn),
            }
        )
    return {
        "status": "ok",
        "mode": settings.mode,
        "backend": "online",
        "build_id": get_build_id(),
        "trueapi": svc.trueapi_state(),
        "organizations": orgs,
    }


@app.post("/api/scan")
async def api_scan(req: ScanRequest):
    result = await svc.scan(req.code, req.org_inn)
    return result.as_dict()


@app.post("/api/scan_batch")
async def api_scan_batch(req: ScanBatchRequest):
    results = await svc.scan_batch(req.codes, req.org_inn)
    return {"results": [r.as_dict() for r in results]}


@app.post("/api/balance")
async def api_balance(req: BalanceRequest):
    try:
        out = await svc.balance(req.gtin)
    except AppError as e:
        return _error_body(e)
    return out


# ---- /admin/token ------------------------------------------------------


def _check_admin(key: Optional[str]) -> bool:
    """Fail-closed: без настроенного ADMIN_KEY доступ запрещён.

    Сравнение через hmac.compare_digest (constant-time, не палит длину ключа).
    """
    return valid_key(key, settings.admin_key)


@app.get("/admin/token")
async def admin_token_page():
    return RedirectResponse('/admin?section=token', status_code=307)


@app.post("/admin/token")
async def admin_token_post(
    request: Request,
    inn: str = Form(...),
    uuid: str = Form(...),
    signature: str = Form(...),
):
    if not authorized(request, settings.admin_key):
        return JSONResponse({"ok": False, "message": "Доступ запрещён"}, status_code=403)
    try:
        token = await svc.exchange_token(inn.strip(), uuid.strip(), signature.strip())
    except AppError as e:
        return JSONResponse({"ok": False, "inn": inn, "message": e.message})
    # НЕ возвращаем сам токен обратно в ответ
    return {"ok": True, "inn": inn.strip(), "message": "Токен обновлён"}


# ---- Warehouse documents ------------------------------------------------
from sales_shipments import install_sales_shipments
from warehouse_store import WarehouseStore
from warehouse_routes import install_warehouse

warehouse = WarehouseStore(settings.warehouse_db)
svc.policy_getter = lambda: warehouse.application_settings()['values']
install_admin_sessions(app, lambda: settings.admin_key)
install_warehouse(app, lambda: svc, lambda: warehouse, templates)
install_sales_shipments(app, lambda: svc, lambda: warehouse, templates)
install_cis_check(app, lambda: svc)


@app.get("/admin/participants", response_class=HTMLResponse, include_in_schema=False)
async def participants_page(request: Request):
    if not authorized(request, settings.admin_key):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request=request, name="participants.html",
        context={"build_id": get_build_id(), "mock": settings.is_mock},
        headers={"Cache-Control": "no-store"},
    )


# ---- static ------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"build_id": get_build_id()},
    )


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
