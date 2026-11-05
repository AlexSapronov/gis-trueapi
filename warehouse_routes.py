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
from models import AppError, ErrorCategory
from buildinfo import get_build_id
from config import settings
from organizations import registry
from warehouse_excel import MAX_FILE_SIZE, export_shipment, import_xlsx, scan_data, template
from warehouse_service import check_document, resolve_pns
from warehouse_store import WarehouseError
from upd_xml import MAX_XML_SIZE, inspect_xml, parse_xml, allocate, enrich_xml, review_plan, check_document as check_upd_document


class ScanBody(BaseModel):
    code: str = Field(min_length=1, max_length=4096)


class GtdBody(ScanBody):
    org_inn: str = Field(min_length=10, max_length=12)


class UpdMapping(BaseModel):
    fill_gtd: bool = False
    mapping: dict[str, str | None] = Field(max_length=5000)
    save_links: bool = False


class DocumentVersion(BaseModel):
    version: int = Field(ge=0)
    revision: int = Field(ge=0)


class ArchiveBody(DocumentVersion):
    confirmation: str = Field(min_length=1, max_length=300)


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
    archived: bool = False


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
        if d['status'] == 'draft' or d['archived_at']:
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

    @admin.post('/gtd')
    async def lookup_gtd(body: GtdBody):
        try:
            return await service_getter().lookup_gtd(body.code, body.org_inn)
        except AppError as exc:
            client_error = exc.category in {
                ErrorCategory.DM_STRUCTURE, ErrorCategory.GTIN_CHECKSUM,
                ErrorCategory.NOT_FOUND, ErrorCategory.NO_ORG_TOKEN,
            }
            # An upstream HTTP 401/403 is not an expired administrator session.
            raise WarehouseError(exc.message, 422 if client_error else 502) from exc

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
                              document_number: str = Form(''), document_date: str = Form(''),
                              file: UploadFile = File(...)):
        content = await file.read(MAX_FILE_SIZE + 1)
        header, lines, expected = await asyncio.to_thread(
            import_xlsx, content, kind, org_inn or None, document_number or None, document_date or None)
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

    @admin.post('/documents/{document_id}/archive')
    def archive_document(document_id: str, body: ArchiveBody):
        return store_getter().archive(document_id, body.version, body.revision, body.confirmation)

    @admin.post('/documents/{document_id}/unarchive')
    def unarchive_document(document_id: str, body: ArchiveBody):
        return store_getter().unarchive(document_id, body.version, body.revision, body.confirmation)

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

    @admin.post('/documents/{document_id}/upd')
    async def upload_upd(document_id: str, file: UploadFile = File(...)):
        content = await file.read(MAX_XML_SIZE + 1)
        store = store_getter()
        doc = store.get(document_id)
        info = await asyncio.to_thread(inspect_xml, content, doc, store.upd_aliases(doc['org_inn']))
        upload_id = store.upd_create(doc, content)
        return {**info, 'upload_id': upload_id}

    @admin.post('/documents/{document_id}/upd/{upload_id}/prepare')
    async def prepare_upd(document_id: str, upload_id: str, body: UpdMapping):
        store = store_getter()
        upload, doc = store.upd_get(document_id, upload_id)
        info = await asyncio.to_thread(inspect_xml, upload['source'], doc, {})
        if any(pn is not None and (not pn.strip() or len(pn) > 1000) for pn in body.mapping.values()):
            raise WarehouseError('Некорректный PN')
        root = await asyncio.to_thread(parse_xml, upload['source'])
        try:
            allocated = allocate(root, doc, body.mapping, regroup=body.fill_gtd)
        except WarehouseError as exc:
            if body.fill_gtd or exc.code != 'upd_pack_boundary':
                raise
            # Without a customs lookup we may suggest new boundaries, but must
            # not transfer a 1C declaration across codes without evidence.
            relaxed = allocate(root, doc, body.mapping, regroup=True)
            plan = review_plan(root, body.mapping, relaxed)
            store.upd_finish(document_id, upload_id, None, None, plan, {})
            return {**plan, 'gtd_filled': False, 'saved_links': 0}

        aliases = {}
        if body.save_links:
            for row in info['rows']:
                source = row['pn'].strip()
                target = body.mapping[row['id']]
                if target is None:
                    continue
                if not source or len(source) > 1000:
                    raise WarehouseError('Для сохранения связи заполните PN из XML (до 1000 символов)')
                if source in aliases and aliases[source] != target:
                    raise WarehouseError('Один PN из XML сопоставлен разным PN склада; снимите сохранение связей')
                aliases[source] = target
        declarations = None
        if body.fill_gtd:
            try:
                async with asyncio.timeout(180):
                    declarations = await service_getter().lookup_gtd_batch(
                        [scan['cis'] for group in allocated for scan in group], doc['org_inn'], allow_missing=True)
            except TimeoutError as exc:
                raise WarehouseError('Истекло время получения ГТД. Повторите подготовку XML.', 504) from exc
            except AppError as exc:
                raise WarehouseError(exc.message, 502) from exc
        if declarations is not None and any(not dt for dt in declarations.values()):
            plan = review_plan(root, body.mapping, allocated, declarations)
            store.upd_finish(document_id, upload_id, None, None, plan, {})
            return {**plan, 'gtd_filled': False, 'saved_links': 0}
        output, filename, summary = await asyncio.to_thread(
            enrich_xml, upload['source'], doc, body.mapping, allocated, declarations)
        store.upd_finish(document_id, upload_id, output, filename, summary, aliases)
        return {'ready': True, 'rows': summary, 'filename': filename, 'saved_links': len(aliases), 'gtd_filled': body.fill_gtd}

    @admin.get('/documents/{document_id}/upd/{upload_id}/download')
    def download_upd(document_id: str, upload_id: str):
        upload, doc = store_getter().upd_get(document_id, upload_id)
        check_upd_document(doc)
        if upload['output'] is None:
            raise WarehouseError('Сначала подготовьте XML', 409)
        return Response(upload['output'], media_type='application/xml', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(upload['filename']),
            'Cache-Control': 'no-store'})

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
