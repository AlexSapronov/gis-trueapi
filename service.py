"""Сервисный слой: единая бизнес-логика для скана, batch и balance.

Service НЕ содержит двух версий логики для mock/live — работает через
MarkingApiClient (Protocol). Только реализация клиента различается.

Клиент возвращает записи info в виде {"cisInfo": {...}, "errorMessage", "errorCode"},
а search — {"items": [...], "is_last": bool, "next": {...}|None}.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from config import settings
from datamatrix import parse
from mockclient import MockTrueApiClient
from models import (
    AppError,
    CATEGORY_MESSAGES,
    ErrorCategory,
    Organization,
    ScanResult,
)
from organizations import registry
from tokens import FileTokenStore, TokenStore
from trueapi import MarkingApiClient, TrueApiError, TrueApiClient, MAX_SEARCH_PER_PAGE


class Service:
    def __init__(self, client: MarkingApiClient, token_store: TokenStore):
        self.client = client
        self.tokens = token_store
        # Состояние последнего обращения к True API (для healthcheck без доп. запросов).
        # Обновляется после каждого scan/batch/balance, НЕ отдельным запросом.
        self._trueapi_last_success: Optional[str] = None   # ISO timestamp
        self._trueapi_last_error: Optional[str] = None     # категория ошибки
        self._trueapi_last_error_time: Optional[str] = None

    async def aclose(self) -> None:
        if hasattr(self.client, "aclose"):
            await self.client.aclose()

    # ---- состояние True API ---------------------------------------------

    def _mark_trueapi_success(self) -> None:
        from datetime import datetime, timezone
        self._trueapi_last_success = datetime.now(timezone.utc).isoformat()
        # успех сбрасывает предыдущую ошибку
        self._trueapi_last_error = None
        self._trueapi_last_error_time = None

    def _mark_trueapi_error(self, category: str) -> None:
        from datetime import datetime, timezone
        self._trueapi_last_error = category
        self._trueapi_last_error_time = datetime.now(timezone.utc).isoformat()

    def trueapi_state(self) -> dict[str, Any]:
        """Безопасная сводка состояния True API для /api/status."""
        if self._trueapi_last_success is None and self._trueapi_last_error is None:
            return {"state": "unknown", "last_success": None, "last_error": None}
        if self._trueapi_last_error is not None and self._trueapi_last_success is None:
            return {
                "state": "error",
                "last_success": None,
                "last_error": self._trueapi_last_error,
                "last_error_time": self._trueapi_last_error_time,
            }
        return {
            "state": "ok",
            "last_success": self._trueapi_last_success,
            "last_error": self._trueapi_last_error,
            "last_error_time": self._trueapi_last_error_time,
        }

    # ---- помощники ------------------------------------------------------

    def _org_token(self, inn: str) -> str:
        token = self.tokens.get(inn)
        if not token:
            raise AppError(
                ErrorCategory.NO_ORG_TOKEN,
                CATEGORY_MESSAGES[ErrorCategory.NO_ORG_TOKEN],
                f"нет токена для ИНН {inn}",
            )
        return token

    def _classify_owner(self, result: ScanResult) -> None:
        if result.owner_inn is None:
            result.ours = None
            return
        org = registry.get(result.owner_inn)
        if org is not None:
            result.ours = True
            result.our_org_name = org.name
        else:
            result.ours = False
            result.our_org_name = None

    @staticmethod
    def _apply_cisinfo(result: ScanResult, rec: dict[str, Any]) -> None:
        """Накладываем cisInfo-запись на ScanResult."""
        ci = rec.get("cisInfo") or {}
        result.product_name = ci.get("productName")
        result.status = ci.get("status")
        result.status_ex = ci.get("statusEx")
        result.owner_inn = ci.get("ownerInn")
        result.owner_name = ci.get("ownerName")
        result.producer_inn = ci.get("producerInn")
        # предпочитаем extendedPackageType / generalPackageType, packageType — устаревший
        result.package_type = (
            ci.get("extendedPackageType")
            or ci.get("generalPackageType")
            or ci.get("packageType")
        )
        result.quantity_in_pack = ci.get("quantityInPack")
        result.raw_api = ci

    @staticmethod
    def _info_error_category(rec: dict[str, Any]) -> str:
        """Категория по errorCode записи info."""
        code = str(rec.get("errorCode") or "")
        if code == "404":
            return ErrorCategory.KM_NOT_FOUND
        if code == "401":
            return ErrorCategory.TOKEN_INVALID
        if code == "403":
            return ErrorCategory.NO_PERMISSION
        if code in ("504",):
            return ErrorCategory.API_UNAVAILABLE
        return ErrorCategory.API_ERROR

    # ---- scan (один КМ) ------------------------------------------------

    async def scan(self, raw_code: str, org_inn: Optional[str] = None) -> ScanResult:
        result = ScanResult(raw_code=raw_code)

        parsed = parse(raw_code)
        result.normalized_cis = parsed.normalized_cis
        result.gtin = parsed.gtin
        result.serial = parsed.serial
        result.structure_valid = parsed.structure_valid
        result.structure_error = parsed.structure_error

        if not parsed.structure_valid:
            result.error_category = (
                ErrorCategory.GTIN_CHECKSUM
                if parsed.structure_error and "контрольн" in parsed.structure_error.lower()
                else ErrorCategory.DM_STRUCTURE
            )
            result.error_message = CATEGORY_MESSAGES[result.error_category]
            return result

        inns = registry.inns()
        if not inns:
            result.error_category = ErrorCategory.NO_ORG_TOKEN
            result.error_message = "Не настроено ни одной организации"
            return result

        use_inn = org_inn if org_inn and registry.has(org_inn) else inns[0]
        try:
            token = self._org_token(use_inn)
        except AppError as e:
            result.error_category = e.category
            result.error_message = e.message
            return result

        api_cis = parsed.api_cis or parsed.normalized_cis
        try:
            records = await self.client.info([api_cis], token)
        except TrueApiError as e:
            result.api_checked = True
            result.error_category = e.category
            result.error_message = e.message
            self._mark_trueapi_error(e.category)
            return result
        except httpx.TimeoutException:
            result.api_checked = True
            result.error_category = ErrorCategory.TIMEOUT
            result.error_message = CATEGORY_MESSAGES[ErrorCategory.TIMEOUT]
            self._mark_trueapi_error(ErrorCategory.TIMEOUT)
            return result
        except Exception:  # noqa: BLE001
            result.api_checked = True
            result.error_category = ErrorCategory.API_ERROR
            result.error_message = CATEGORY_MESSAGES[ErrorCategory.API_ERROR]
            self._mark_trueapi_error(ErrorCategory.API_ERROR)
            return result

        # До этого места — контакт с True API состоялся (сервер ответил).
        self._mark_trueapi_success()

        if not records:
            result.api_checked = True
            result.error_category = ErrorCategory.KM_NOT_FOUND
            result.error_message = CATEGORY_MESSAGES[ErrorCategory.KM_NOT_FOUND]
            return result

        rec = records[0]
        result.api_checked = True

        # запись может содержать индивидуальную ошибку по этому КИ
        if rec.get("errorMessage"):
            result.error_category = self._info_error_category(rec)
            result.error_message = rec["errorMessage"]
            # при 422/owner — владелец недоступен, но не ошибка структуры КМ
            return result

        self._apply_cisinfo(result, rec)
        self._classify_owner(result)
        return result

    # ---- batch ---------------------------------------------------------

    async def scan_batch(
        self, lines: list[str], org_inn: Optional[str] = None
    ) -> list[ScanResult]:
        cleaned = [ln.strip() for ln in lines if ln.strip()]
        results: list[ScanResult] = []
        valid: list[tuple[int, str]] = []

        for idx, raw in enumerate(cleaned):
            sr = ScanResult(raw_code=raw)
            parsed = parse(raw)
            sr.normalized_cis = parsed.normalized_cis
            sr.gtin = parsed.gtin
            sr.serial = parsed.serial
            sr.structure_valid = parsed.structure_valid
            sr.structure_error = parsed.structure_error
            if parsed.structure_valid:
                valid.append((idx, parsed.api_cis or parsed.normalized_cis))
            else:
                sr.error_category = (
                    ErrorCategory.GTIN_CHECKSUM
                    if parsed.structure_error and "контрольн" in parsed.structure_error.lower()
                    else ErrorCategory.DM_STRUCTURE
                )
                sr.error_message = CATEGORY_MESSAGES[sr.error_category]
            results.append(sr)

        if not valid:
            return results

        inns = registry.inns()
        if not inns:
            for sr in results:
                sr.error_category = ErrorCategory.NO_ORG_TOKEN
                sr.error_message = "Не настроено ни одной организации"
            return results
        use_inn = org_inn if org_inn and registry.has(org_inn) else inns[0]

        try:
            token = self._org_token(use_inn)
        except AppError as e:
            for sr in results:
                if sr.structure_valid:
                    sr.error_category = e.category
                    sr.error_message = e.message
            return results

        cises = [c for _, c in valid]
        try:
            records = await self.client.info(cises, token)
        except TrueApiError as e:
            for idx, _ in valid:
                results[idx].api_checked = True
                results[idx].error_category = e.category
                results[idx].error_message = e.message
            self._mark_trueapi_error(e.category)
            return results
        except httpx.TimeoutException:
            for idx, _ in valid:
                results[idx].api_checked = True
                results[idx].error_category = ErrorCategory.TIMEOUT
                results[idx].error_message = CATEGORY_MESSAGES[ErrorCategory.TIMEOUT]
            self._mark_trueapi_error(ErrorCategory.TIMEOUT)
            return results
        except Exception:  # noqa: BLE001
            for idx, _ in valid:
                results[idx].api_checked = True
                results[idx].error_category = ErrorCategory.API_ERROR
                results[idx].error_message = CATEGORY_MESSAGES[ErrorCategory.API_ERROR]
            self._mark_trueapi_error(ErrorCategory.API_ERROR)
            return results

        # контакт с True API состоялся
        self._mark_trueapi_success()

        # сопоставляем записи с КИ по requestedCis
        by_cis: dict[str, dict[str, Any]] = {}
        for rec in records:
            ci = rec.get("cisInfo") or {}
            key = ci.get("requestedCis") or ci.get("cis")
            if key:
                by_cis[key] = rec

        for idx, cis in valid:
            sr = results[idx]
            sr.api_checked = True
            rec = by_cis.get(cis)
            if rec is None:
                sr.error_category = ErrorCategory.KM_NOT_FOUND
                sr.error_message = CATEGORY_MESSAGES[ErrorCategory.KM_NOT_FOUND]
                continue
            if rec.get("errorMessage"):
                sr.error_category = self._info_error_category(rec)
                sr.error_message = rec["errorMessage"]
                continue
            self._apply_cisinfo(sr, rec)
            self._classify_owner(sr)

        return results

    # ---- balance -------------------------------------------------------

    async def balance(
        self, gtin: str, statuses: list[str] | None = None
    ) -> dict[str, Any]:
        from datamatrix import gtin_checksum_valid

        if not gtin_checksum_valid(gtin):
            raise AppError(
                ErrorCategory.GTIN_CHECKSUM,
                CATEGORY_MESSAGES[ErrorCategory.GTIN_CHECKSUM],
            )

        statuses = statuses or ["EMITTED", "APPLIED", "INTRODUCED"]
        orgs = registry.all()

        async def _org_balance(org: Organization) -> dict[str, Any]:
            out: dict[str, Any] = {
                "inn": org.inn,
                "name": org.name,
                "statuses": {},
                "error": None,
            }
            token = self.tokens.get(org.inn)
            if not token:
                out["error"] = CATEGORY_MESSAGES[ErrorCategory.NO_ORG_TOKEN]
                return out
            try:
                for st in statuses:
                    # пагинация по lastEmissionDate + sgtin
                    all_items: list[dict[str, Any]] = []
                    after = None
                    while True:
                        page = await self.client.search(
                            gtin, st, token,
                            per_page=MAX_SEARCH_PER_PAGE,
                            after=after,
                            product_groups=settings.product_groups,
                        )
                        items = page.get("items", []) or []
                        all_items.extend(items)
                        if page.get("is_last") or not page.get("next"):
                            break
                        after = page["next"]
                    # quantityInPack получаем через info batch
                    qty_sum = 0
                    cises = [it.get("cis") or it.get("sgtin") for it in all_items if (it.get("cis") or it.get("sgtin"))]
                    if cises:
                        records = await self.client.info(cises, token)
                        for rec in records:
                            if rec.get("errorMessage"):
                                continue
                            ci = rec.get("cisInfo") or {}
                            try:
                                qty_sum += int(ci.get("quantityInPack") or 0)
                            except (TypeError, ValueError):
                                pass
                    out["statuses"][st] = {
                        "km_count": len(all_items),
                        "quantity_sum": qty_sum,
                    }
            except TrueApiError as e:
                out["error"] = e.message or CATEGORY_MESSAGES.get(e.category, str(e))
            except Exception as e:  # noqa: BLE001
                out["error"] = CATEGORY_MESSAGES[ErrorCategory.API_ERROR]
            return out

        results = await asyncio.gather(*(_org_balance(o) for o in orgs))

        # Состояние True API: контакт состоялся, если хоть одна организация с
        # токеном успешно обработала (или ЧЗ ответил, даже если кодов нет).
        # Если организации с токеном есть и ВСЕ упали с API-ошибкой интерпретируем
        # как ошибку ЧЗ только если в результатах нет ни одного "нет токена" как
        # единственной причины (нет токена — это НЕ ошибка ЧЗ).
        any_ok = any(r["error"] is None for r in results)
        any_token = any(self.tokens.has(o.inn) for o in orgs)
        if any_ok:
            self._mark_trueapi_success()
        elif any_token and any(r["error"] for r in results):
            # все организации с токеном дали ошибку — но это может быть и 403
            # (нет прав), не обязательно сеть. Отмечаем ошибкой API.
            self._mark_trueapi_error(ErrorCategory.API_ERROR)

        total: dict[str, dict[str, int]] = {
            st: {"km_count": 0, "quantity_sum": 0} for st in statuses
        }
        for r in results:
            for st in statuses:
                if st in r["statuses"]:
                    total[st]["km_count"] += r["statuses"][st]["km_count"]
                    total[st]["quantity_sum"] += r["statuses"][st]["quantity_sum"]

        return {
            "gtin": gtin,
            "statuses": statuses,
            "total": total,
            "organizations": results,
        }

    # ---- exchange token -------------------------------------------------

    async def exchange_token(self, inn: str, uuid: str, signature: str) -> str:
        if not registry.has(inn):
            raise AppError(ErrorCategory.NOT_FOUND, "Организация не найдена")
        try:
            token = await self.client.exchange_token(inn, uuid, signature)
        except TrueApiError as e:
            raise AppError(
                e.category,
                e.message or CATEGORY_MESSAGES.get(e.category, "Ошибка авторизации"),
            )
        self.tokens.set(inn, token)
        return token


def build_service() -> Service:
    if settings.is_mock:
        client = MockTrueApiClient()
    else:
        client = TrueApiClient(settings.base_url)
    store: TokenStore = FileTokenStore(settings.tokens_file)
    return Service(client, store)