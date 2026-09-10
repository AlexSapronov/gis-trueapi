"""Regression-тесты на business policy layer (verdict).

Проверяют, что интерпретация статуса ЧЗ отделена от факта успешного
выполнения запроса к True API:

- APPLIED / INTRODUCED                 -> verdict=ok (зелёный)
- EMITTED                              -> verdict=warning (жёлтый)
- неизвестный/новый статус             -> verdict=warning (жёлтый), без смысла
- ошибка структуры Data Matrix         -> verdict=error (красный)
- EMITTED при успешном True API НЕ меняет глобальное состояние True API на error
- batch со смешанными статусами даёт корректный расклад total/ok/warning/error.
"""
from __future__ import annotations

import pytest

from mockclient import MockTrueApiClient
from models import (
    ErrorCategory,
    VERTICT_ERROR,
    VERTICT_OK,
    VERTICT_WARNING,
    business_verdict,
)
from service import Service
from tokens import FileTokenStore

VALID_GTIN = "04640638345218"


@pytest.fixture
def svc(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.set("7805809291", "mock-token")
    client = MockTrueApiClient()
    s = Service(client, store)
    yield s


# ---- business_verdict (чистая функция) ------------------------------------

class TestBusinessVerdictFn:
    def test_applied_is_ok(self):
        assert business_verdict("APPLIED") == (VERTICT_OK, None)

    def test_introduced_is_ok(self):
        assert business_verdict("INTRODUCED") == (VERTICT_OK, None)

    def test_emitted_is_warning(self):
        verdict, msg = business_verdict("EMITTED")
        assert verdict == VERTICT_WARNING
        assert "нанесение" in msg

    def test_unknown_status_is_warning(self):
        verdict, msg = business_verdict("SOME_FUTURE_STATUS")
        assert verdict == VERTICT_WARNING
        assert "SOME_FUTURE_STATUS" in msg

    def test_empty_status_is_error(self):
        assert business_verdict(None) == (VERTICT_ERROR, None)
        assert business_verdict("") == (VERTICT_ERROR, None)

    def test_status_normalized_case(self):
        # статус может прийти в разном регистре
        assert business_verdict("applied") == (VERTICT_OK, None)
        assert business_verdict("emitted")[0] == VERTICT_WARNING


# ---- verdict в ScanResult через Service.scan -------------------------------

class TestScanVerdict:
    @pytest.mark.asyncio
    async def test_applied_verdict_ok(self, svc):
        r = await svc.scan(f"01{VALID_GTIN}21APPLIED01")
        assert r.status == "APPLIED"
        assert r.verdict == VERTICT_OK

    @pytest.mark.asyncio
    async def test_introduced_verdict_ok(self, svc):
        r = await svc.scan(f"01{VALID_GTIN}21INTRODUCED01")
        assert r.status == "INTRODUCED"
        assert r.verdict == VERTICT_OK

    @pytest.mark.asyncio
    async def test_emitted_verdict_warning(self, svc):
        r = await svc.scan(f"01{VALID_GTIN}21EMITTED01")
        assert r.status == "EMITTED"
        assert r.verdict == VERTICT_WARNING
        assert r.verdict_message is not None
        # НЕ ошибка — error_category пуст
        assert r.error_category is None

    @pytest.mark.asyncio
    async def test_bad_structure_verdict_error(self, svc):
        r = await svc.scan("garbage")
        assert r.structure_valid is False
        assert r.verdict == VERTICT_ERROR
        assert r.error_category == ErrorCategory.DM_STRUCTURE

    @pytest.mark.asyncio
    async def test_emitted_does_not_change_trueapi_state_to_error(self, svc):
        # сначала успешный APPLIED — state=ok
        await svc.scan(f"01{VALID_GTIN}21APPLIED01")
        assert svc.trueapi_state()["state"] == "ok"

        # теперь EMITTED — успешный ответ True API, но warning по КМ
        r = await svc.scan(f"01{VALID_GTIN}21EMITTED01")
        assert r.verdict == VERTICT_WARNING
        # глобальное состояние True API остаётся ok (не error)
        assert svc.trueapi_state()["state"] == "ok"
        assert svc.trueapi_state()["last_error"] is None


# ---- verdict как поле REST (as_dict) ---------------------------------------

class TestVerdictSerialized:
    def test_as_dict_includes_verdict(self):
        from models import ScanResult
        sr = ScanResult(raw_code="x")
        sr.verdict = VERTICT_WARNING
        sr.verdict_message = "m"
        d = sr.as_dict()
        assert d["verdict"] == VERTICT_WARNING
        assert d["verdict_message"] == "m"


# ---- batch со смешанными статусами ----------------------------------------

class TestBatchVerdict:
    @pytest.mark.asyncio
    async def test_batch_mixed_verdict_distribution(self, svc):
        code_applied = f"01{VALID_GTIN}21APP1"
        code_introduced = f"01{VALID_GTIN}21INTRODUCED1"
        code_emitted = f"01{VALID_GTIN}21EMITTED1"
        bad = "junk"

        results = await svc.scan_batch(
            [code_applied, code_introduced, code_emitted, bad]
        )

        assert len(results) == 4

        verdicts = [r.verdict for r in results]
        assert verdicts.count(VERTICT_OK) == 2
        assert verdicts.count(VERTICT_WARNING) == 1
        assert verdicts.count(VERTICT_ERROR) == 1

        # расклад по факту
        emitted = [r for r in results if r.verdict == VERTICT_WARNING][0]
        assert emitted.status == "EMITTED"
        assert emitted.error_category is None

        bad_row = [r for r in results if r.verdict == VERTICT_ERROR][0]
        assert bad_row.structure_valid is False
        assert bad_row.error_category == ErrorCategory.DM_STRUCTURE


# ---- неизвестный статус через mock ----------------------------------------

class _UnknownStatusClient(MockTrueApiClient):
    """Mock, который возвращает неизвестный статус для конкретного serial."""

    async def info(self, cises, token):
        records = await super().info(cises, token)
        for rec in records:
            ci = rec.get("cisInfo") or {}
            if "MYSTERY" in (ci.get("requestedCis") or ""):
                ci["status"] = "MYSTERY"
        return records


class TestUnknownStatusViaService:
    @pytest.mark.asyncio
    async def test_unknown_status_is_warning_not_ok(self, tmp_path):
        store = FileTokenStore(tmp_path / "t.json")
        store.set("7805809291", "mock-token")
        client = _UnknownStatusClient()
        svc_local = Service(client, store)

        r = await svc_local.scan(f"01{VALID_GTIN}21MYSTERY01")
        assert r.status == "MYSTERY"
        assert r.verdict == VERTICT_WARNING
        assert r.verdict_message is not None
        assert "MYSTERY" in r.verdict_message
        # не зелёный и не красный error_category
        assert r.error_category is None