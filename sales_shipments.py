"""Passwordless access to a manager's own shipment tasks."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from admin_auth import SESSION_SECONDS, issue_session, valid_session
from buildinfo import get_build_id
from config import settings
from warehouse_excel import MAX_FILE_SIZE, template
from warehouse_routes import xlsx_response
from warehouse_service import import_document

API_PATH = '/api/sales-shipments'
COOKIE_NAME = 'gis_sales_session'
PRIVATE_HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                   'X-Robots-Tag': 'noindex, nofollow'}


class SalesLogin(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    link_key: str = Field(min_length=32, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


def session_key(username, link_key):
    return f'sales-shipments:{username}:{link_key}'


def install_sales_shipments(app, service_getter, store_getter, templates):
    def enabled():
        if not settings.sales_shipment_links:
            raise HTTPException(404, 'Раздел недоступен', headers=PRIVATE_HEADERS)

    def check_request(request: Request):
        if request.headers.get('X-Sales-Request') != '1':
            raise HTTPException(403, 'Доступ запрещён', headers=PRIVATE_HEADERS)

    def current_username(request: Request):
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie:
            for username, link_key in settings.sales_shipment_links.items():
                if valid_session(cookie, session_key(username, link_key)):
                    return username
        raise HTTPException(401, 'Откройте персональную ссылку и войдите снова', headers=PRIVATE_HEADERS)

    @app.get('/sales-shipments', response_class=HTMLResponse, include_in_schema=False,
             dependencies=[Depends(enabled)])
    async def page(request: Request):
        return templates.TemplateResponse(
            request=request, name='sales_shipments.html', headers=PRIVATE_HEADERS,
            context={'build_id': get_build_id()})

    router = APIRouter(prefix=API_PATH, include_in_schema=False, dependencies=[Depends(enabled)])

    @router.post('/session', dependencies=[Depends(check_request)])
    def login(body: SalesLogin, request: Request):
        username = body.username.strip().casefold()
        key = settings.sales_shipment_links.get(username)
        if key is None or not secrets.compare_digest(key, body.link_key):
            raise HTTPException(403, 'Имя пользователя не соответствует персональной ссылке',
                                headers=PRIVATE_HEADERS)
        response = JSONResponse({'username': username}, headers=PRIVATE_HEADERS)
        response.set_cookie(COOKIE_NAME, issue_session(session_key(username, key)), max_age=SESSION_SECONDS,
                            httponly=True, secure=request.url.scheme == 'https', samesite='strict', path=API_PATH)
        return response

    @router.get('/session')
    def session(username: str = Depends(current_username)):
        return JSONResponse({'username': username}, headers=PRIVATE_HEADERS)

    @router.delete('/session', dependencies=[Depends(check_request)])
    def logout(request: Request):
        response = JSONResponse({'ok': True}, headers=PRIVATE_HEADERS)
        response.delete_cookie(COOKIE_NAME, httponly=True, secure=request.url.scheme == 'https',
                               samesite='strict', path=API_PATH)
        return response

    @router.get('/template', dependencies=[Depends(current_username)])
    def shipment_template():
        return xlsx_response(template('shipment'), 'shipment-template.xlsx')

    @router.get('/tasks')
    def tasks(username: str = Depends(current_username)):
        return JSONResponse({'tasks': store_getter().sales_shipments(username)}, headers=PRIVATE_HEADERS)

    @router.post('/tasks', status_code=201, dependencies=[Depends(check_request)])
    async def create_task(file: UploadFile = File(...), username: str = Depends(current_username)):
        document = await import_document(store_getter(), service_getter(), await file.read(MAX_FILE_SIZE + 1),
                                         'shipment', sales_username=username)
        return JSONResponse(document, status_code=201, headers=PRIVATE_HEADERS)

    app.include_router(router)
