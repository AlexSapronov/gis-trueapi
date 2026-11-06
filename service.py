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
from datamatrix import parse, strip_ai30
from gtd import interpret_gtd_record
from mockclient import MockTrueApiClient
from models import (
    AppError,
    CATEGORY_MESSAGES,
    ErrorCategory,
    Organization,
    ScanResult,
    business_verdict,
)
from organizations import registry
from tokens import FileTokenStore, TokenStore
from trueapi import MarkingApiClient, TrueApiError, TrueApiClient, MAX_SEARCH_PER_PAGE


class Service:
    def __init__(self, client: MarkingApiClient, token_store: TokenStore):
        self.client = client
        self.tokens = token_store
        self.policy_getter = lambda: {}
        # Состояние последнего обращения к True API (для healthcheck без доп. запросов).
        # Обновляется после каждого scan/batch/balance, НЕ отдельным запросом.
        # `_trueapi_state` — ТЕКУЩЕЕ состояние ("unknown"/"ok"/"error"),
        # timestamps хранятся отдельно и при этом НЕ стираются.
        self._trueapi_state: str = "unknown"
        self._trueapi_last_success: Optional[str] = None   # ISO timestamp (не стирается)
        self._trueapi_last_error: Optional[str] = None     # категория ПОСЛЕДНЕЙ ошибки
        self._trueapi_last_error_time: Optional[str] = None

    async def aclose(self) -> None:
        if hasattr(self.client, "aclose"):
            await self.client.aclose()

    # ---- состояние True API ---------------------------------------------

    def _mark_trueapi_success(self) -> None:
        from datetime import datetime, timezone
        self._trueapi_state = "ok"
        self._trueapi_last_success = datetime.now(timezone.utc).isoformat()
        # успех сбрасывает только «текущую ошибку», но не timestamp последнего успеха
        self._trueapi_last_error = None
        self._trueapi_last_error_time = None

    def _mark_trueapi_error(self, category: str) -> None:
        from datetime import datetime, timezone
        self._trueapi_state = "error"
        self._trueapi_last_error = category
        self._trueapi_last_error_time = datetime.now(timezone.utc).isoformat()

    def trueapi_state(self) -> dict[str, Any]:
        """Безопасная сводка состояния True API для /api/status.

        state определяется по ПОСЛЕДНЕМУ событию (success/error), а не по
        наличию/отсутствию last_success. Timestamps выдаются отдельно.
        """
        return {
            "state": self._trueapi_state,
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

    def _pick_org_inn(self, org_inn: Optional[str]) -> str:
        """Выбирает ИНН организации для запроса к True API.

        - Если org_inn передан: организация должна существовать (иначе ошибка),
          используется именно она.
        - Если org_inn НЕ передан: выбирается ПЕРВАЯ организация с реально
          настроенным токеном (а не просто первая в реестре).
        """
        if org_inn is not None:
            if not registry.has(org_inn):
                raise AppError(
                    ErrorCategory.NOT_FOUND,
                    f"Организация с ИНН {org_inn} не найдена",
                )
            return org_inn
        # без явного выбора — первая организация с токеном
        for inn in registry.inns():
            if self.tokens.has(inn):
                return inn
        # токена нет ни у одной организации
        inns = registry.inns()
        raise AppError(
            ErrorCategory.NO_ORG_TOKEN,
            "Не настроено ни одной организации с токеном True API",
            f"организации: {inns or 'нет'}",
        )

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
    def _apply_cisinfo(result: ScanResult, rec: dict[str, Any], policy=None) -> None:
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

        # business policy layer: интерпретация фактического статуса ЧЗ.
        # НЕ влияет на глобальное состояние True API (оно помечается отдельно).
        result.verdict, result.verdict_message = business_verdict(result.status, policy)

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

    # ---- administrator: customs declaration lookup by scanned KI --------

    # /api/status describes core cises/info/search operations. Optional
    # cises/short/list failures/successes must not overwrite the scanner health.
    async def lookup_gtd_batch(self, cises: list[str], org_inn: str, *, allow_missing: bool = False) -> dict[str, str | None]:
        """Resolve every KI; never associate reordered short/list replies by position."""
        token = self._org_token(self._pick_org_inn(org_inn))
        expected = set(cises)
        identities_to_cis = {key: cis for cis in expected
                             for key in (cis, cis[2:16] + cis[18:])}
        try:
            records = await self.client.short_info(list(dict.fromkeys(cises)), token)
        except TrueApiError as exc:
            raise AppError(exc.category, exc.message) from exc
        except Exception as exc:
            raise AppError(ErrorCategory.API_ERROR, 'Не удалось получить ГТД из ЧЗ') from exc
        found = {}
        for record in records:
            details = record.get('result') or record.get('cisInfo') or {}
            nested = details.get('cisInfo') if isinstance(details, dict) else None
            identities = [record.get('requestedCis'), record.get('cis')]
            for source in (details, nested):
                if isinstance(source, dict):
                    identities.extend([source.get('requestedCis'), source.get('cis')])
            supplied = [key for key in identities if isinstance(key, str) and key]
            keys = {identities_to_cis[key] for key in supplied if key in identities_to_cis}
            if len(keys) != 1 or any(key not in identities_to_cis for key in supplied):
                raise AppError(ErrorCategory.API_ERROR, 'В ответе ЧЗ нет однозначной связи КИ и ГТД')
            cis = keys.pop()
            if cis in found:
                raise AppError(ErrorCategory.API_ERROR, 'ЧЗ вернул повторный ответ по КИ ' + cis)
            try:
                result = interpret_gtd_record(record, cis, cis[2:16])
            except ValueError as exc:
                raise AppError(ErrorCategory.API_ERROR, str(exc)) from exc
            dt = result.get('declaration_number')
            if not result.get('found'):
                raise AppError(ErrorCategory.API_ERROR, 'КИ не найден при запросе ГТД: ' + cis)
            if dt is not None and not isinstance(dt, str):
                raise AppError(ErrorCategory.API_ERROR, 'Некорректный номер ГТД для КИ ' + cis)
            if not dt or not dt.strip():
                if not allow_missing:
                    raise AppError(ErrorCategory.API_ERROR, 'ГТД не получена для КИ ' + cis)
                dt = None
            if result.get('gtin') != cis[2:16]:
                raise AppError(ErrorCategory.API_ERROR, 'GTIN в ответе ЧЗ не совпадает с КИ ' + cis)
            found[cis] = dt.strip() if dt else None
        missing = expected - found.keys()
        if missing:
            raise AppError(ErrorCategory.API_ERROR, 'ЧЗ не вернул ГТД для КИ ' + sorted(missing)[0])
        return found

    async def lookup_km_report_batch(self, cises: list[str], org_inn: str) -> dict[str, dict]:
        """Read quantityInPack via info and DT via short/list, paired by KI."""
        token = self._org_token(self._pick_org_inn(org_inn))
        aliases = {key: cis for cis in cises for key in (cis, cis[2:16] + cis[18:])}
        found = {cis: dict(quantity_in_pack=None, declaration_number=None, status=None, message='') for cis in cises}

        def pair(records):
            paired = {}
            for record in records:
                details = record.get('result') or record.get('cisInfo') or {}
                nested = details.get('cisInfo') if isinstance(details, dict) else None
                identities = [record.get('requestedCis'), record.get('cis')]
                for source in (details, nested):
                    if isinstance(source, dict):
                        identities.extend([source.get('requestedCis'), source.get('cis')])
                supplied = [key for key in identities if isinstance(key, str) and key]
                keys = {aliases[key] for key in supplied if key in aliases}
                if len(keys) != 1 or any(key not in aliases for key in supplied):
                    raise ValueError('В ответе ЧЗ нет однозначной связи КИ и сведений')
                cis = keys.pop()
                if cis in paired:
                    raise ValueError('ЧЗ вернул повторный ответ по КИ')
                paired[cis] = record
            return paired

        for method, label in ((self.client.info, 'Количество'), (self.client.short_info, 'ГТД')):
            try:
                records = pair(await method(cises, token))
            except Exception as exc:
                message = str(exc) if isinstance(exc, (ValueError, AppError, TrueApiError)) else 'Не удалось получить сведения из ЧЗ'
                for result in found.values():
                    result['message'] += label + ': ' + message + '; '
                continue
            for cis, result in found.items():
                record = records.get(cis)
                try:
                    if record is None:
                        raise ValueError('ЧЗ не вернул сведения по КИ')
                    if record.get('errorMessage') or record.get('errorCode'):
                        raise ValueError(str(record.get('errorMessage') or 'Ошибка ЧЗ ' + str(record['errorCode'])))
                    if label == 'Количество':
                        details = record.get('cisInfo') or {}
                        if details.get('gtin') != cis[2:16]:
                            raise ValueError('GTIN в ответе ЧЗ не совпадает с КИ')
                        result['status'] = details.get('status')
                        qty = details.get('quantityInPack')
                        if qty is None:
                            raise ValueError('quantityInPack не указан в ЧЗ')
                        if isinstance(qty, bool) or not str(qty).isascii() or not str(qty).isdigit() or not 0 < int(qty) <= 2147483647:
                            raise ValueError('Некорректное quantityInPack в ЧЗ')
                        result['quantity_in_pack'] = int(qty)
                    else:
                        info = interpret_gtd_record(record, cis, cis[2:16])
                        dt = info.get('declaration_number')
                        if info.get('gtin') != cis[2:16] or (dt is not None and not isinstance(dt, str)):
                            raise ValueError('GTIN или номер ГТД в ответе ЧЗ некорректен')
                        if not dt or not dt.strip():
                            raise ValueError('Номер ГТД не указан в доступных сведениях ЧЗ')
                        result['declaration_number'] = dt.strip()
                except ValueError as exc:
                    result['message'] += label + ': ' + str(exc) + '; '
        for result in found.values():
            result['message'] = result['message'].rstrip('; ') or 'Сведения получены из ЧЗ'
        return found

    async def lookup_gtd(self, raw_code: str, org_inn: Optional[str] = None) -> dict[str, Any]:
        """Read DT information via True API without exposing tokens or raw responses."""
        parsed = parse(strip_ai30(raw_code))
        if not parsed.structure_valid or not parsed.api_cis:
            category = (
                ErrorCategory.GTIN_CHECKSUM
                if parsed.structure_error and "контрольн" in parsed.structure_error.lower()
                else ErrorCategory.DM_STRUCTURE
            )
            raise AppError(category, parsed.structure_error or CATEGORY_MESSAGES[category])

        use_inn = self._pick_org_inn(org_inn)
        token = self._org_token(use_inn)
        try:
            records = await self.client.short_info([parsed.api_cis], token)
        except TrueApiError as exc:
            if exc.status == 404:
                raise AppError(
                    ErrorCategory.API_ERROR,
                    'True API вернул HTTP 404 при обращении к cises/short/list. '
                    'Проверьте доступность метода на используемом контуре ЧЗ. '
                    'Сам по себе этот ответ не означает, что КМ не найден.',
                ) from exc
            raise AppError(exc.category, exc.message) from exc
        except httpx.TimeoutException as exc:
            raise AppError(ErrorCategory.TIMEOUT, CATEGORY_MESSAGES[ErrorCategory.TIMEOUT]) from exc
        except Exception as exc:
            raise AppError(ErrorCategory.API_ERROR, CATEGORY_MESSAGES[ErrorCategory.API_ERROR]) from exc
        if not records:
            return {
                "found": False, "cis": parsed.api_cis, "gtin": parsed.gtin,
                "declaration_number": None, "declaration_date": None,
                "declaration_id": None, "message": "КИ не найден в ответе True API",
            }
        try:
            return interpret_gtd_record(records[0], parsed.api_cis, parsed.gtin)
        except ValueError as exc:
            raise AppError(ErrorCategory.API_ERROR, str(exc)) from exc

    # ---- scan (один КМ) ------------------------------------------------

    async def scan(self, raw_code: str, org_inn: Optional[str] = None) -> ScanResult:
        policy = self.policy_getter()
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

        try:
            use_inn = self._pick_org_inn(org_inn)
        except AppError as e:
            result.error_category = e.category
            result.error_message = e.message
            return result

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

        self._apply_cisinfo(result, rec, policy)
        self._classify_owner(result)
        return result

    # ---- batch ---------------------------------------------------------

    async def scan_batch(
        self, lines: list[str], org_inn: Optional[str] = None
    ) -> list[ScanResult]:
        policy = self.policy_getter()
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

        try:
            use_inn = self._pick_org_inn(org_inn)
        except AppError as e:
            for sr in results:
                if sr.structure_valid:
                    sr.error_category = e.category
                    sr.error_message = e.message
            return results

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
            self._apply_cisinfo(sr, rec, policy)
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
            }
            token = self.tokens.get(org.inn)
            if not token:
                # нет токена — организация полностью недоступна; статусы не заполняем,
                # помечаем на уровне организации. Такая организация НЕ тянет вниз total.
                out["error"] = CATEGORY_MESSAGES[ErrorCategory.NO_ORG_TOKEN]
                out["no_token"] = True
                return out

            for st in statuses:
                # каждый статус считается независимо: свой результат/ошибка/полнота
                try:
                    all_items: list[dict[str, Any]] = []
                    after = None
                    seen_markers: set[tuple] = set()
                    while True:
                        page = await self.client.search(
                            gtin, st, token,
                            per_page=MAX_SEARCH_PER_PAGE,
                            after=after,
                            product_groups=settings.product_groups,
                        )
                        items = page.get("items", []) or []
                        all_items.extend(items)
                        if page.get("is_last"):
                            break
                        nxt = page.get("next")
                        if not nxt:
                            # is_last=false, но continuation marker отсутствует =>
                            # результат неполный, это ошибка пагинации, не успех.
                            raise TrueApiError(
                                ErrorCategory.API_SERVER_ERROR,
                                "Неполная пагинация: отсутствует continuation marker",
                            )
                        # защита от зацикливания на одном и том же маркере
                        marker = (nxt.get("lastEmissionDate"), nxt.get("sgtin") or nxt.get("cis"))
                        if marker in seen_markers:
                            raise TrueApiError(
                                ErrorCategory.API_SERVER_ERROR,
                                "Повторяющийся continuation marker (зацикливание пагинации)",
                            )
                        seen_markers.add(marker)
                        after = nxt

                    qty_sum = 0
                    if st != "EMITTED":
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
                        "complete": True,
                        "error": None,
                    }
                except TrueApiError as e:
                    out["statuses"][st] = {
                        "km_count": None, "quantity_sum": None,
                        "complete": False,
                        "error": e.message or CATEGORY_MESSAGES.get(e.category, str(e)),
                    }
                except Exception as e:  # noqa: BLE001
                    out["statuses"][st] = {
                        "km_count": None, "quantity_sum": None,
                        "complete": False,
                        "error": CATEGORY_MESSAGES[ErrorCategory.API_ERROR],
                    }
            return out

        results = await asyncio.gather(*(_org_balance(o) for o in orgs))

        # Состояние True API по последнему событию.
        # Контакт успешен, если хоть один (токен, статус) вернул complete без ошибки.
        any_complete = any(
            any(s.get("complete") and not s.get("error") for s in r["statuses"].values())
            for r in results
        )
        any_token = any(self.tokens.has(o.inn) for o in orgs)
        if any_complete:
            self._mark_trueapi_success()
        elif any_token:
            self._mark_trueapi_error(ErrorCategory.API_ERROR)

        # Общий итог: km_count/quantity_sum считаем только по организациям С ТОКЕНОМ,
        # и только по их полным статусам. Организации без токена не включаются в итог
        # (у них нет доступа), но перечисляются на org-уровне в organizations.
        # Если по полному статусу данных нет у орг с токеном — помечаем неполным.
        total: dict[str, dict[str, Any]] = {}
        for st in statuses:
            km = 0
            qty = 0
            complete = True
            failed_orgs: list[str] = []
            for r in results:
                if r.get("no_token"):
                    continue
                entry = r["statuses"].get(st)
                if entry is None:
                    complete = False
                    failed_orgs.append(r["name"])
                    continue
                if entry.get("error") or not entry.get("complete"):
                    complete = False
                    failed_orgs.append(r["name"])
                    continue
                km += entry["km_count"] or 0
                qty += entry["quantity_sum"] or 0
            total[st] = {
                "km_count": km if complete else (km or None),
                "quantity_sum": qty if complete else (qty or None),
                "complete": complete,
                "failed_organizations": failed_orgs,
            }

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
