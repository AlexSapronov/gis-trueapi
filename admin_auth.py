"""Shared administrator session: signed HttpOnly cookie, no browser key storage."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

COOKIE_NAME = 'gis_admin_session'
SESSION_SECONDS = 8 * 60 * 60


def valid_key(provided, configured):
    if not configured or configured == 'change-me-in-production' or not provided:
        return False
    return hmac.compare_digest(provided.encode('utf-8'), configured.encode('utf-8'))


def issue_session(configured):
    payload = f'{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}'
    signature = hmac.new(configured.encode(), ('admin-session:' + payload).encode(), hashlib.sha256).hexdigest()
    return payload + '.' + signature


def valid_session(value, configured):
    if not value or not configured or configured == 'change-me-in-production':
        return False
    try:
        expiry, nonce, signature = value.split('.')
        if len(nonce) != 32 or len(signature) != 64:
            return False
        if not time.time() < int(expiry) <= time.time() + SESSION_SECONDS:
            return False
        payload = expiry + '.' + nonce
        expected = hmac.new(configured.encode(), ('admin-session:' + payload).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def authorized(request, configured):
    key = request.headers.get('X-Admin-Key')
    if key is not None:
        return valid_key(key, configured)
    if not valid_session(request.cookies.get(COOKIE_NAME), configured):
        return False
    # Cross-origin forms cannot add this header; CORS is not enabled for external sites.
    return request.method in ('GET', 'HEAD', 'OPTIONS') or request.headers.get('X-Admin-Request') == '1'


class LoginBody(BaseModel):
    key: str = Field(min_length=1, max_length=4096)


def install_admin_sessions(app, key_getter):
    @app.post('/api/admin/session')
    async def login(body: LoginBody, request: Request):
        configured = key_getter()
        if not valid_key(body.key, configured):
            raise HTTPException(403, 'Неверный ключ администратора')
        response = JSONResponse({'ok': True}, headers={'Cache-Control': 'no-store'})
        response.set_cookie(COOKIE_NAME, issue_session(configured), max_age=SESSION_SECONDS,
                            httponly=True, secure=request.url.scheme == 'https', samesite='strict', path='/')
        return response

    @app.get('/api/admin/session')
    async def session(request: Request):
        if not authorized(request, key_getter()):
            raise HTTPException(403, 'Войдите в административный интерфейс')
        return JSONResponse({'ok': True}, headers={'Cache-Control': 'no-store'})

    @app.delete('/api/admin/session')
    async def logout(request: Request):
        if request.headers.get('X-Admin-Request') != '1':
            raise HTTPException(403, 'Доступ запрещён')
        response = JSONResponse({'ok': True}, headers={'Cache-Control': 'no-store'})
        response.delete_cookie(COOKIE_NAME, httponly=True, secure=request.url.scheme == 'https',
                               samesite='strict', path='/')
        return response
