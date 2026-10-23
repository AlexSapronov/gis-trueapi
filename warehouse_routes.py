"""Warehouse HTTP boundary; administrator sessions are shared with token management."""
from __future__ import annotations

import asyncio
import re
from datetime import date
from urllib.parse import quote
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from pydantic import BaseModel, Field

from admin_auth import authorized
from app_settings import ApplicationSettings, SettingsUpdate, defaults
from buildinfo import get_build_id
from config import settings
from organizations import registry
from warehouse_excel import MAX_FILE_SIZE, export_shipment, import_xlsx, scan_data, template
from warehouse_service import check_document, resolve_pns
from warehouse_store import WarehouseError


class ScanBody(BaseModel):
    code: str = Field(min_length=1, max_length=4096)


class DocumentVersion(BaseModel):
    version: int = Field(ge=0)
    revision: int = Field(ge=0)


class EditBody(DocumentVersion):
    quantities: dict[str, Annotated[int, Field(strict=True, gt=0, le=2147483647)]] = Field(default_factory=dict)
    strict_owner: bool | None = None
    pns: dict[str, str] = Field(default_factory=dict)


class WarehouseFilters(BaseModel):
    kind: Literal['shipment','receipt'] | None = None
    org_inn: str | None = Field(default=None,max_length=12)
    scan_state: Literal['all','unscanned','scanned'] = 'all'
    status: Literal['draft','active','completed'] | None = None
    query: str = Field(default='',max_length=300)
    date_from: date | None = None
    date_to: date | None = None


class AdminFilters(WarehouseFilters):
    issue_state: Literal['all','open','closed','none'] = 'all'
    trash: bool = False


class ReportBody(BaseModel):
    version: int = Field(ge=0)
    result_at: str = Field(min_length=1,max_length=100)
    message: str = Field(default='',max_length=2000)


class ResolveBody(BaseModel):
    resolution: str = Field(min_length=1,max_length=2000)


class ReturnBody(ResolveBody):
    request_id: str = Field(min_length=1,max_length=64,pattern=r'^[a-zA-Z0-9_-]+$')


def install_warehouse(app, service_getter, store_getter, templates):
    router = APIRouter()

    def auth(request: Request):
        if not authorized(request, settings.admin_key):
            raise HTTPException(403, 'Доступ запрещён')

    def visible(document_id):
        d = store_getter().get(document_id)
        if d['status'] == 'draft':
            raise WarehouseError('Документ недоступен складу', 404)
        return d

    @app.exception_handler(WarehouseError)
    async def warehouse_error(request, exc):
        from fastapi.responses import JSONResponse
        body = {'message': exc.message}
        if exc.code:
            body['code'] = exc.code
        return JSONResponse(body, status_code=exc.status)

    @router.get('/warehouse', response_class=HTMLResponse)
    async def warehouse_page(request: Request):
        return templates.TemplateResponse(request=request, name='warehouse.html',
                                          context={'build_id': get_build_id(), 'admin': False})

    @router.get('/admin/warehouse')
    async def legacy_admin_page(request: Request):
        kind = 'receipt' if request.query_params.get('kind') == 'receipt' else 'shipment'
        target = '/admin?kind=' + kind
        if request.query_params.get('doc'):
            target += '&doc=' + quote(request.query_params['doc'], safe='')
        return RedirectResponse(target, status_code=307)

    @router.get('/admin', response_class=HTMLResponse)
    async def admin_page(request: Request):
        # This shell contains no data or secrets. Data/actions require a session or API key.
        return templates.TemplateResponse(request=request, name='warehouse.html',
                                          context={'build_id': get_build_id(), 'admin': True})

    @router.get('/api/warehouse/documents')
    def documents(filters: Annotated[WarehouseFilters, Query()]):
        if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
            raise WarehouseError('Начало периода позже окончания')
        return {'documents': store_getter().list(**filters.model_dump())}

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

    @router.post('/api/warehouse/documents/{document_id}/issues')
    def report_issue(document_id: str, body: ReportBody):
        visible(document_id)
        return store_getter().report_issue(document_id,body.version,body.result_at,body.message)

    @router.post('/api/warehouse/documents/{document_id}/returns/{reply_id}/acknowledge')
    def acknowledge_return(document_id: str,reply_id: str):
        visible(document_id)
        return store_getter().acknowledge_return(document_id,reply_id)

    admin = APIRouter(prefix='/api/admin/warehouse', dependencies=[Depends(auth)])

    @admin.get('/settings')
    def admin_settings():
        runtime = store_getter().application_settings()
        return {'organizations': [{'inn': o.inn, 'name': o.name} for o in registry.all()],
                'receipt_default_org': runtime['values']['receipt_default_org'],
                'runtime': runtime,
                'defaults': defaults(), 'schema': ApplicationSettings.model_json_schema(),
                'history': store_getter().settings_history()}

    @admin.put('/settings')
    def save_settings(body: SettingsUpdate):
        return store_getter().update_settings(body.revision, body.values.model_dump())

    @admin.get('/documents')
    def admin_documents(filters: Annotated[AdminFilters, Query()]):
        if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
            raise WarehouseError('Начало периода позже окончания')
        return {'documents': store_getter().list(admin=True, **filters.model_dump())}

    @admin.get('/templates/{kind}')
    def download_template(kind: str):
        return xlsx_response(template(kind, store_getter().application_settings()['values']['receipt_default_org'] if kind == 'receipt' else ''), kind + '-template.xlsx')

    @admin.post('/documents')
    async def create_document(kind: str = Form(...), org_inn: str = Form(''),
                              file: UploadFile = File(...)):
        content = await file.read(MAX_FILE_SIZE + 1)
        header, lines, expected = await asyncio.to_thread(import_xlsx, content, kind, org_inn or None)
        warnings = []
        values = store_getter().application_settings()['values']
        if kind == 'shipment' and values['auto_pn']:
            try:
                async with asyncio.timeout(90):
                    pns, warnings = await resolve_pns(service_getter(), [l['gtin'] for l in lines if l['gtin'] and not l['pn']], header['org_inn'])
            except TimeoutError:
                pns, warnings = {}, ['Получение PN заняло слишком много времени. Заполните PN вручную.']
            for line in lines:
                if not line['pn'] and line['gtin'] in pns:
                    line.update(pn=pns[line['gtin']], pn_source='trueapi')
        d = store_getter().create(header, lines, expected)
        return {**d, 'import_warnings': warnings}

    @admin.get('/documents/{document_id}')
    def admin_document(document_id: str):
        return store_getter().get(document_id,include_deleted=True)

    @admin.patch('/documents/{document_id}')
    def edit(document_id: str, body: EditBody):
        if any(len(pn) > 1000 for pn in body.pns.values()):
            raise WarehouseError('PN слишком длинный')
        return store_getter().edit(document_id, body.version, body.strict_owner, body.pns, body.quantities, body.revision)

    @admin.delete('/documents/{document_id}')
    def delete_document(document_id: str, body: DocumentVersion):
        store_getter().delete(document_id, body.version, body.revision)
        return {'deleted': True}

    @admin.post('/documents/{document_id}/restore')
    def restore_document(document_id: str, body: DocumentVersion):
        return store_getter().restore(document_id,body.version,body.revision)

    @admin.get('/documents/{document_id}/issues')
    def issues(document_id: str):
        return {'issues':store_getter().issues(document_id)}

    @admin.get('/documents/{document_id}/issues/{issue_id}')
    def issue(document_id: str,issue_id: str):
        return store_getter().issue(document_id,issue_id)

    @admin.get('/documents/{document_id}/issues/{issue_id}/events')
    def issue_events(document_id: str,issue_id: str,before: int | None = Query(None,ge=1)):
        return store_getter().issue_events(document_id,issue_id,before)

    @admin.post('/documents/{document_id}/issues/{issue_id}/resolve')
    def resolve_issue(document_id: str,issue_id: str,body: ResolveBody):
        return store_getter().resolve_issue(document_id,issue_id,body.resolution)

    @admin.post('/documents/{document_id}/issues/{issue_id}/return')
    def return_to_warehouse(document_id: str,issue_id: str,body: ReturnBody):
        return store_getter().return_to_warehouse(document_id,issue_id,body.resolution,body.request_id)

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
