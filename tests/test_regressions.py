"""Regression-тесты по итогам code review.

Покрывают:
- последовательности состояния True API (success/error переходы);
- выбор организации с токеном (не первая в реестре);
- balance: для EMITTED info() не вызывается;
- raw_api не попадает в обычный as_dict();
- batch-валидация (<=1000, длина кода, GTIN ровно 14 цифр).
"""
from __future__ import annotations

import pytest

from mockclient import MockTrueApiClient
from models import ErrorCategory, ScanResult
from organizations import registry
from schemas import BalanceRequest, ScanBatchRequest, ScanRequest
from service import Service
from tokens import FileTokenStore
from trueapi import TrueApiError

VALID_GTIN = "04640638345218"


@pytest.fixture
def svc(tmp_path):
    store = FileTokenStore(tmp_path / "tokens.json")
    store.set("7805809291", "mock-token")
    store.set("7805550962", "mock-token-2")
    store.set("7805809950", "mock-token-3")
    store.set("7805825462", "mock-token-4")
    client = MockTrueApiClient()
    s = Service(client, store)
    yield s


# ---- Состояние True API ---------------------------------------------------

class TestTrueApiState:
    def test_initial_state_unknown(self, svc):
        st = svc.trueapi_state()
        assert st["state"] == "unknown"
        assert st["last_success"] is None
        assert st["last_error"] is None

    def test_unknown_then_success(self, svc):
        svc._mark_trueapi_success()
        assert svc.trueapi_state()["state"] == "ok"

    def test_success_then_error(self, svc):
        svc._mark_trueapi_success()
        svc._mark_trueapi_error(ErrorCategory.TIMEOUT)
        st = svc.trueapi_state()
        assert st["state"] == "error"
        # timestamp последнего успеха должен сохраняться
        assert st["last_success"] is not None

    def test_error_then_success(self, svc):
        svc._mark_trueapi_error(ErrorCategory.TIMEOUT)
        svc._mark_trueapi_success()
        st = svc.trueapi_state()
        assert st["state"] == "ok"
        assert st["last_error"] is None

    def test_success_error_success(self, svc):
        svc._mark_trueapi_success()
        svc._mark_trueapi_error(ErrorCategory.API_SERVER_ERROR)
        assert svc.trueapi_state()["state"] == "error"
        svc._mark_trueapi_success()
        st = svc.trueapi_state()
        assert st["state"] == "ok"
        assert st["last_error"] is None
        # last_success не затирается на промежуточном error и обновляется на success
        assert st["last_success"] is not None


# ---- Выбор организации с токеном -------------------------------------------

class TestOrgSelection:
    @pytest.mark.asyncio
    async def test_scan_uses_first_org_with_token(self, tmp_path):
        # У КОМБРИ (первая в реестре) токена НЕТ, у МТ-СИСТЕМС — есть.
        store = FileTokenStore(tmp_path / "t.json")
        store.set("7805550962", "mt-token")  # МТ-СИСТЕМС
        svc_local = Service(MockTrueApiClient(), store)
        code = f"01{VALID_GTIN}21SCANX"
        r = await svc_local.scan(code)
        # запрос должен пройти (не "нет токена"), потому что нашёлся доступный токен
        assert r.error_category is None
        assert r.structure_valid is True

    @pytest.mark.asyncio
    async def test_scan_no_token_anywhere(self, tmp_path):
        store = FileTokenStore(tmp_path / "t.json")  # пусто
        svc_local = Service(MockTrueApiClient(), store)
        code = f"01{VALID_GTIN}21SCANX"
        r = await svc_local.scan(code)
        assert r.error_category == ErrorCategory.NO_ORG_TOKEN

    @pytest.mark.asyncio
    async def test_scan_explicit_org_without_token(self, tmp_path):
        store = FileTokenStore(tmp_path / "t.json")
        store.set("7805550962", "mt-token")
        svc_local = Service(MockTrueApiClient(), store)
        # явно просим КОМБРИ, у которой токена нет
        r = await svc_local.scan(f"01{VALID_GTIN}21SCANX", org_inn="7805809291")
        assert r.error_category == ErrorCategory.NO_ORG_TOKEN

    @pytest.mark.asyncio
    async def test_scan_explicit_unknown_org(self, tmp_path):
        store = FileTokenStore(tmp_path / "t.json")
        store.set("7805550962", "mt-token")
        svc_local = Service(MockTrueApiClient(), store)
        r = await svc_local.scan(f"01{VALID_GTIN}21SCANX", org_inn="9999999999")
        assert r.error_category == ErrorCategory.NOT_FOUND


# ---- balance: EMITTED без info() ------------------------------------------

class _RecordingClient(MockTrueApiClient):
    """Mock, который записывает вызовы info() для проверки оптимизаций."""

    def __init__(self):
        super().__init__()
        self.info_calls: list[str] = []   # статусы, для которых вызван info
        self._last_status = None

    async def search(self, gtin, status, token, per_page=1000, after=None, product_groups=None):
        self._last_status = status
        return await super().search(gtin, status, token, per_page, after, product_groups)

    async def info(self, cises, token):
        # регистрируем, для какого статуса вызывается info (по последнему search)
        self.info_calls.append(self._last_status)
        return await super().info(cises, token)


class TestBalanceEmittedSkipInfo:
    @pytest.mark.asyncio
    async def test_emitted_does_not_call_info(self, tmp_path):
        client = _RecordingClient()
        store = FileTokenStore(tmp_path / "t.json")
        store.set("7805809291", "mock-token")
        svc_local = Service(client, store)
        await svc_local.balance(VALID_GTIN, statuses=["EMITTED", "APPLIED", "INTRODUCED"])
        # info() не должен вызываться для EMITTED
        assert "EMITTED" not in client.info_calls
        # для APPLIED и INTRODUCED — вызывается
        assert "APPLIED" in client.info_calls
        assert "INTRODUCED" in client.info_calls


# ---- raw_api не в обычном ответе -------------------------------------------

class TestRawApiExcluded:
    def test_as_dict_no_raw_api_by_default(self):
        sr = ScanResult(raw_code="x", gtin="04640638345218")
        sr.raw_api = {"cisInfo": {"productName": "secret-detail"}}
        d = sr.as_dict()
        assert "raw_api" not in d

    def test_as_dict_raw_api_opt_in(self):
        sr = ScanResult(raw_code="x")
        sr.raw_api = {"cisInfo": {}}
        d = sr.as_dict(include_raw_api=True)
        assert "raw_api" in d


# ---- batch-валидация -------------------------------------------------------

class TestBatchValidation:
    def test_batch_over_1000_rejected(self):
        codes = [f"01{VALID_GTIN}21X{i}" for i in range(1001)]
        with pytest.raises(Exception):
            ScanBatchRequest(codes=codes)

    def test_batch_1000_accepted(self):
        codes = [f"01{VALID_GTIN}21X{i}" for i in range(1000)]
        req = ScanBatchRequest(codes=codes)
        assert len(req.codes) == 1000

    def test_scan_code_too_long_rejected(self):
        with pytest.raises(Exception):
            ScanRequest(code="0" * 600)

    def test_balance_gtin_must_be_14_digits(self):
        with pytest.raises(Exception):
            BalanceRequest(gtin="123")
        with pytest.raises(Exception):
            BalanceRequest(gtin="123456789012345")  # 15 цифр
        # ровно 14 цифр — ок
        assert BalanceRequest(gtin=VALID_GTIN).gtin == VALID_GTIN