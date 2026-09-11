"""Интерфейс клиента к True API + live-реализация.

Backend — единственная точка общения с True API. Frontend никогда не видит
токены и не обращается к True API напрямую.

Схема методов — по официальной документации «Описание True API» (ГИС МТ),
версия 721.0:
- auth:      GET /auth/key, POST /auth/simpleSignIn
- info:      POST /api/v3/true-api/cises/info  (массив КИ, до 1000)
- search:    POST /api/v4/true-api/cises/search (фильтр + пагинация)
- check:     POST /cises/check — криптографическая верификация КМ.

ВАЖНО: `check()` НЕ используется в основном flow (scan/batch/balance) — там
применяется только `info()` (сведения по КИ), который криптографической
проверкой НЕ является. `check()` оставлен как реализованный, но не
подключённый метод — перед подключением нужно сверить с актуальной
документацией его URL, формат запроса и дополнительные лимиты/запросы.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx

from models import CATEGORY_MESSAGES, ErrorCategory

logger = logging.getLogger("gis.trueapi")

# Auth (реальный прод-путь включает префикс /api/v3/true-api, как в refresh_trueapi_token.ps1)
AUTH_KEY_PATH = "/api/v3/true-api/auth/key"
AUTH_SIGN_IN_PATH = "/api/v3/true-api/auth/simpleSignIn"
# Cises
INFO_PATH = "/api/v3/true-api/cises/info"
SEARCH_PATH = "/api/v4/true-api/cises/search"
CHECK_PATH = "/cises/check"

MAX_INFO_BATCH = 1000
MAX_SEARCH_PER_PAGE = 1000


class MarkingApiClient(Protocol):
    """Общий интерфейс клиента к ГИС МТ / True API."""

    async def info(self, cises: list[str], token: str) -> list[dict[str, Any]]:
        """Сведения по КМ (batch). Возвращает список объектов cisInfo-уровня."""
        ...

    async def search(
        self,
        gtin: str,
        status: str,
        token: str,
        per_page: int = MAX_SEARCH_PER_PAGE,
        after: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Поиск КМ по GTIN + статусу.

        Возвращает {items: [...], is_last: bool, next: {lastEmissionDate, sgtin} | None}.
        """
        ...

    async def check(self, cises: list[str], token: str) -> dict[str, Any]:
        """Криптографическая проверка КМ."""
        ...

    async def exchange_token(self, inn: str, uuid: str, signature: str) -> str:
        """Обмен UUID+SIGNATURE на bearer-токен. Возвращает токен."""
        ...


class TrueApiError(Exception):
    """Ошибка, категоризованная по HTTP-статусу/типу."""

    def __init__(self, category: str, message: str, status: int | None = None):
        self.category = category
        self.message = message
        self.status = status
        super().__init__(message)


def _category_for_status(status: int) -> str:
    if status == 401:
        return ErrorCategory.UNAUTHORIZED
    if status == 403:
        return ErrorCategory.FORBIDDEN
    if status == 404:
        return ErrorCategory.NOT_FOUND
    if status == 422:
        return ErrorCategory.VALIDATION
    if status == 429:
        return ErrorCategory.RATE_LIMIT
    if 500 <= status < 600:
        return ErrorCategory.API_SERVER_ERROR
    return ErrorCategory.API_ERROR


class TrueApiClient:
    """Live-клиент True API через httpx.AsyncClient (connection pooling)."""

    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    # ---- низкий уровень -------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _post_json(
        self, path: str, body: Any, token: str | None = None
    ) -> httpx.Response:
        headers = {"Content-Type": "application/json", "Accept": "*/*"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return await self.http.post(self._url(path), json=body, headers=headers)
        except httpx.TimeoutException:
            raise TrueApiError(ErrorCategory.TIMEOUT, CATEGORY_MESSAGES[ErrorCategory.TIMEOUT])
        except httpx.ConnectError:
            raise TrueApiError(
                ErrorCategory.API_UNAVAILABLE,
                CATEGORY_MESSAGES[ErrorCategory.API_UNAVAILABLE],
            )

    async def _get_json(self, path: str) -> httpx.Response:
        headers = {"Accept": "application/json"}
        try:
            return await self.http.get(self._url(path), headers=headers)
        except httpx.TimeoutException:
            raise TrueApiError(ErrorCategory.TIMEOUT, CATEGORY_MESSAGES[ErrorCategory.TIMEOUT])
        except httpx.ConnectError:
            raise TrueApiError(
                ErrorCategory.API_UNAVAILABLE,
                CATEGORY_MESSAGES[ErrorCategory.API_UNAVAILABLE],
            )

    def _raise_http(self, resp: httpx.Response) -> None:
        """Поднять TrueApiError по HTTP-статусу при общей ошибке запроса."""
        cat = _category_for_status(resp.status_code)
        msg = ""
        try:
            data = resp.json()
            msg = data.get("error_message", "")
        except Exception:
            msg = resp.text[:200]
        raise TrueApiError(cat, msg or CATEGORY_MESSAGES[cat], resp.status_code)

    def _raise_401(self, resp: httpx.Response) -> None:
        # 401 => токен недействителен/истёк
        raise TrueApiError(
            ErrorCategory.TOKEN_INVALID,
            "Токен недействителен или истёк",
            resp.status_code,
        )

    # ---- auth -----------------------------------------------------------

    async def exchange_token(self, inn: str, uuid: str, signature: str) -> str:
        """signature — подписанные УКЭП данные (base64) из refresh_trueapi_token.ps1."""
        body = {
            "uuid": uuid,
            "data": signature,
        }
        if inn:
            body["inn"] = inn
        resp = await self._post_json(AUTH_SIGN_IN_PATH, body, token=None)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get("token") or data.get("uuidToken")
            if not token:
                raise TrueApiError(
                    ErrorCategory.API_ERROR, "В ответе нет токена", 200
                )
            return token
        if resp.status_code in (401, 403):
            self._raise_401(resp)
        self._raise_http(resp)

    # ---- info -----------------------------------------------------------

    async def info(self, cises: list[str], token: str) -> list[dict[str, Any]]:
        """Отправляет список КИ (кусками по 1000) и возвращает записи.

        Каждый элемент результата — dict вида
        {"cisInfo": {...}, "errorMessage": str|None, "errorCode": str|None},
        с добавлением локального ключа "_category" (категория ошибки).
        """
        results: list[dict[str, Any]] = []
        for start in range(0, len(cises), MAX_INFO_BATCH):
            chunk = cises[start : start + MAX_INFO_BATCH]
            resp = await self._post_json(INFO_PATH, chunk, token=token)
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, list):
                    data = []
                for item in data:
                    if isinstance(item, dict):
                        results.append(item)
                continue
            if resp.status_code == 401:
                self._raise_401(resp)
            if resp.status_code in (403, 422):
                self._raise_http(resp)
            # 404/400: иногда сервер возвращает ИМЕННО тела со списком ошибок по КИ
            # (напр. "все КИ не найдены" → HTTP 404 + [{cisInfo, errorMessage, errorCode}]).
            # Это НЕ ошибка соединения: контакт с ЧЗ состоялся. Возвращаем список как есть,
            # чтобы Service обработал каждый КИ индивидуально как «не найден».
            if resp.status_code in (400, 404):
                try:
                    data = resp.json()
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                results.append(item)
                        continue
                except Exception:
                    pass
                self._raise_http(resp)
            # 5xx и прочее
            self._raise_http(resp)
        return results

    # ---- search ---------------------------------------------------------

    async def search(
        self,
        gtin: str,
        status: str,
        token: str,
        per_page: int = MAX_SEARCH_PER_PAGE,
        after: dict[str, Any] | None = None,
        product_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        """Поиск по GTIN + статусу. `after` — маркер страницы {lastEmissionDate, sgtin}."""
        per_page = min(per_page, MAX_SEARCH_PER_PAGE)
        filter_body: dict[str, Any] = {"gtins": [gtin]}
        if status:
            filter_body["states"] = [{"status": status}]
        if product_groups:
            filter_body["productGroups"] = product_groups

        pagination: dict[str, Any] = {"perPage": per_page, "direction": 0}
        if after:
            pagination["lastEmissionDate"] = after.get("lastEmissionDate")
            pagination["sgtin"] = after.get("sgtin")
        body = {"filter": filter_body, "pagination": pagination}

        resp = await self._post_json(SEARCH_PATH, body, token=token)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("result", []) or []
            is_last = bool(data.get("isLastPage", True))
            # формируем маркер следующей страницы из последнего элемента
            next_marker = None
            if not is_last and items:
                last = items[-1]
                next_marker = {
                    "lastEmissionDate": last.get("emissionDate"),
                    "sgtin": last.get("sgtin") or last.get("cis"),
                }
            return {"items": items, "is_last": is_last, "next": next_marker}
        if resp.status_code == 401:
            self._raise_401(resp)
        self._raise_http(resp)

    # ---- check ----------------------------------------------------------

    async def check(self, cises: list[str], token: str) -> dict[str, Any]:
        resp = await self._post_json(CHECK_PATH, {"codes": cises}, token=token)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 401:
            self._raise_401(resp)
        self._raise_http(resp)