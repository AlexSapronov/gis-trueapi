"""Authenticated client for the internal km-split service."""
from __future__ import annotations

import httpx


class KmSplitError(Exception):
    pass


class KmSplitClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = (base_url or '').rstrip('/')
        self.token = token or ''
        self.enabled = bool(self.base_url and self.token)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self):
        await self._http.aclose()

    def _headers(self):
        if not self.enabled:
            raise KmSplitError('Автоматическое разделение не настроено: задайте KM_SPLIT_URL и KM_SPLIT_INTERNAL_TOKEN')
        return {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}

    async def _request(self, method, path, body=None):
        try:
            response = await self._http.request(
                method, self.base_url + path, headers=self._headers(), json=body)
        except httpx.TimeoutException as exc:
            raise KmSplitError('Истекло время ожидания km-split. Результат внешней операции нужно проверить, повторная отправка запрещена.') from exc
        except httpx.HTTPError as exc:
            raise KmSplitError('Сервис автоматического разделения недоступен') from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get('detail') or payload.get('message') or ''
            except Exception:
                message = ''
            raise KmSplitError(str(message)[:1000] or f'km-split вернул HTTP {response.status_code}')
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KmSplitError('km-split вернул некорректный ответ') from exc

    async def create(self, code: str, parts: list[int], request_id: str):
        return await self._request('POST', '/api/internal/splits',
                                   {'code': code, 'parts': parts, 'request_id': request_id})

    async def status(self, batch_id: str):
        return await self._request('GET', '/api/internal/splits/' + batch_id)

    async def confirm(self, batch_id: str, scanned_cis: list[str]):
        return await self._request('POST', '/api/internal/splits/' + batch_id + '/confirm',
                                   {'scanned_cis': scanned_cis})

    async def labels(self, batch_id: str) -> bytes:
        if not self.enabled:
            raise KmSplitError('Автоматическое разделение не настроено')
        try:
            response = await self._http.get(
                self.base_url + '/api/internal/splits/' + batch_id + '/labels.pdf',
                headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/pdf'})
        except httpx.HTTPError as exc:
            raise KmSplitError('Не удалось получить PDF этикеток из km-split') from exc
        if response.status_code >= 400:
            raise KmSplitError('km-split не готов отдать этикетки (HTTP ' + str(response.status_code) + ')')
        if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/pdf':
            raise KmSplitError('km-split вернул не PDF')
        return response.content
