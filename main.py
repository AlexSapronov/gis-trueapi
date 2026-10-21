"""FastAPI-приложение: внутренний REST API + static + /admin/token.

Frontend никогда не получает bearer-токены и не обращается к True API.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from admin_auth import authorized, install_admin_sessions, valid_key
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

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="GIS MT True API — internal tool")

svc: Service = build_service()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
from warehouse_store import WarehouseStore
from warehouse_routes import install_warehouse

warehouse = WarehouseStore(settings.warehouse_db)
svc.policy_getter = lambda: warehouse.application_settings()['values']
install_admin_sessions(app, lambda: settings.admin_key)
install_warehouse(app, lambda: svc, lambda: warehouse, templates)


# ---- static ------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"build_id": get_build_id()},
    )


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
