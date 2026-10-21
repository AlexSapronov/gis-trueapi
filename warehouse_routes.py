"""Warehouse HTTP boundary; administrator secrets are accepted in a header only."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from buildinfo import get_build_id
from config import settings
from organizations import registry
from warehouse_excel import MAX_FILE_SIZE, export_shipment, import_xlsx, scan_data, template
from warehouse_service import check_document, resolve_pns
from warehouse_store import WarehouseError


class ScanBody(BaseModel):
    code: str = Field(min_length=1, max_length=4096)


class EditBody(BaseModel):
    version: int = Field(ge=0)
    revision: int = Field(ge=0)
    quantities: dict[str, Annotated[int, Field(strict=True, gt=0, le=2147483647)]] = Field(default_factory=dict)
    strict_owner: bool | None = None
    pns: dict[str, str] = Field(default_factory=dict)


def install_warehouse(app, service_getter, store_getter, admin_check, templates):
    router = APIRouter()

    def auth(request: Request):
        if not admin_check(request.headers.get('X-Admin-Key')):
            raise HTTPException(403, 'Доступ запрещён')

    def visible(document_id):
        d = store_getter().get(document_id)
        if d['status'] == 'draft':
            raise WarehouseError('Документ недоступен складу', 404)
        return d

    @app.exception_handler(WarehouseError)
    async def warehouse_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'message': exc.message}, status_code=exc.status)

    @router.get('/warehouse', response_class=HTMLResponse)
    async def warehouse_page(request: Request):
        return templates.TemplateResponse(request=request, name='warehouse.html',
                                          context={'build_id': get_build_id(), 'admin': False})

    @router.get('/admin/warehouse', response_class=HTMLResponse)
    async def admin_page(request: Request):
        # This shell contains no data or secrets. All data/actions require the header.
        return templates.TemplateResponse(request=request, name='warehouse.html',
                                          context={'build_id': get_build_id(), 'admin': True})

    @router.get('/api/warehouse/documents')
    def documents(kind: str | None = None):
        return {'documents': store_getter().list(kind=kind)}

    @router.get('/api/warehouse/documents/{document_id}')
    def document(document_id: str):
        return visible(document_id)

    @router.post('/api/warehouse/documents/{document_id}/scans')
    def add_scan(document_id: str, body: ScanBody):
        visible(document_id)
        return store_getter().add_scan(document_id, scan_data(body.code))

    @router.delete('/api/warehouse/documents/{document_id}/scans/{scan_id}')
    def delete_scan(document_id: str, scan_id: str):
        visible(document_id)
        return store_getter().delete_scan(document_id, scan_id)

    @router.post('/api/warehouse/documents/{document_id}/check')
    async def check(document_id: str):
        visible(document_id)
        try:
            return await check_document(store_getter(), service_getter(), document_id)
        except TimeoutError:
            raise WarehouseError('Превышено время проверки. Повторите проверку.', 504)

    @router.post('/api/warehouse/documents/{document_id}/complete')
    def complete(document_id: str):
        visible(document_id)
        return store_getter().complete(document_id)

    admin = APIRouter(prefix='/api/admin/warehouse', dependencies=[Depends(auth)])

    @admin.get('/settings')
    def admin_settings():
        return {'organizations': [{'inn': o.inn, 'name': o.name} for o in registry.all()],
                'receipt_default_org': settings.receipt_default_org}

    @admin.get('/documents')
    def admin_documents(kind: str | None = None):
        return {'documents': store_getter().list(admin=True, kind=kind)}

    @admin.get('/templates/{kind}')
    def download_template(kind: str):
        return xlsx_response(template(kind, settings.receipt_default_org if kind == 'receipt' else ''), kind + '-template.xlsx')

    @admin.post('/documents')
    async def create_document(kind: str = Form(...), org_inn: str = Form(''),
                              file: UploadFile = File(...)):
        content = await file.read(MAX_FILE_SIZE + 1)
        header, lines, expected = await asyncio.to_thread(import_xlsx, content, kind, org_inn or None)
        warnings = []
        if kind == 'shipment':
            try:
                async with asyncio.timeout(90):
                    pns, warnings = await resolve_pns(service_getter(), [l['gtin'] for l in lines if not l['pn']], header['org_inn'])
            except TimeoutError:
                pns, warnings = {}, ['Получение PN заняло слишком много времени. Заполните PN вручную.']
            for line in lines:
                if not line['pn'] and line['gtin'] in pns:
                    line.update(pn=pns[line['gtin']], pn_source='trueapi')
        d = store_getter().create(header, lines, expected)
        return {**d, 'import_warnings': warnings}

    @admin.get('/documents/{document_id}')
    def admin_document(document_id: str):
        return store_getter().get(document_id)

    @admin.patch('/documents/{document_id}')
    def edit(document_id: str, body: EditBody):
        if any(len(pn) > 1000 for pn in body.pns.values()):
            raise WarehouseError('PN слишком длинный')
        return store_getter().edit(document_id, body.version, body.strict_owner, body.pns, body.quantities, body.revision)

    @admin.post('/documents/{document_id}/start')
    def start(document_id: str):
        return store_getter().start(document_id)

    @admin.post('/documents/{document_id}/reopen')
    def reopen(document_id: str):
        return store_getter().start(document_id, reopen=True)

    @admin.get('/documents/{document_id}/export')
    def export(document_id: str):
        d = store_getter().get(document_id)
        number = re.sub(r'[^\w.-]', '_', d['number'])[:100]
        return xlsx_response(export_shipment(d), 'Отгрузка_' + number + '_КМ.xlsx')

    app.include_router(router)
    app.include_router(admin)


def xlsx_response(content, filename):
    return Response(content, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename),
                             'Cache-Control': 'no-store'})
