"""Тесты Service на mock-клиенте."""
import pytest

from mockclient import MockTrueApiClient
from models import ErrorCategory
from tokens import FileTokenStore
from service import Service

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


@pytest.mark.asyncio
async def test_scan_our_code(svc):
    code = f"01{VALID_GTIN}21SERIAL1"
    r = await svc.scan(code)
    assert r.structure_valid is True
    assert r.ours is True
    assert r.our_org_name == 'ООО "КОМБРИ"'
    assert r.product_name == "M12D-04PFFS-SF8002"
    assert r.status == "APPLIED"


@pytest.mark.asyncio
async def test_scan_foreign_code(svc):
    # чужой GTIN (валидный по checksum)
    code = "010771234567890721" + "SERIAL"
    r = await svc.scan(code)
    assert r.structure_valid is True
    assert r.ours is False


@pytest.mark.asyncio
async def test_scan_status_emitted(svc):
    code = f"01{VALID_GTIN}21EMITTED01"
    r = await svc.scan(code)
    assert r.status == "EMITTED"


@pytest.mark.asyncio
async def test_scan_introduced(svc):
    code = f"01{VALID_GTIN}21INTRODUCED01"
    r = await svc.scan(code)
    assert r.status == "INTRODUCED"


@pytest.mark.asyncio
async def test_scan_not_found(svc):
    code = f"01{VALID_GTIN}21NOTFOUND00"
    r = await svc.scan(code)
    assert r.error_category == ErrorCategory.KM_NOT_FOUND


@pytest.mark.asyncio
async def test_scan_bad_structure(svc):
    r = await svc.scan("garbage")
    assert r.structure_valid is False
    assert r.error_category == ErrorCategory.DM_STRUCTURE


@pytest.mark.asyncio
async def test_scan_bad_checksum(svc):
    # GTIN с неверной контрольной цифрой (07712345678909 — последняя цифра не та)
    code = "010771234567890921" + "SERIAL"
    r = await svc.scan(code)
    assert r.structure_valid is False
    assert r.error_category == ErrorCategory.GTIN_CHECKSUM


@pytest.mark.asyncio
async def test_scan_quantity(svc):
    code = f"01{VALID_GTIN}21Q500"
    r = await svc.scan(code)
    assert r.quantity_in_pack == 500


@pytest.mark.asyncio
async def test_batch_mixed(svc):
    valid = f"01{VALID_GTIN}21AAA"
    invalid = "junk123"
    lines = [valid, "", invalid]
    results = await svc.scan_batch(lines)
    assert len(results) == 2  # пустая строка удалена
    assert results[0].structure_valid is True
    assert results[1].structure_valid is False


@pytest.mark.asyncio
async def test_batch_duplicates(svc):
    code = f"01{VALID_GTIN}21DUP"
    results = await svc.scan_batch([code, code])
    assert len(results) == 2
    assert all(r.structure_valid for r in results)


@pytest.mark.asyncio
async def test_balance_independent(svc):
    out = await svc.balance(VALID_GTIN)
    total = out["total"]
    # независимые счётчики из mock: 12 / 17 / 5
    assert total["EMITTED"]["km_count"] == 12 * 4  # 4 организации
    assert total["APPLIED"]["km_count"] == 17 * 4
    assert total["INTRODUCED"]["km_count"] == 5 * 4


@pytest.mark.asyncio
async def test_balance_one_org_error(svc, tmp_path):
    # новый чистый store: токены есть у 3 организаций, у Поинт-Л — нет
    import os
    fresh = FileTokenStore(tmp_path / "fresh.json")
    fresh.set("7805809291", "mock-token")
    fresh.set("7805550962", "mock-token-2")
    fresh.set("7805809950", "mock-token-3")
    svc.tokens = fresh
    out = await svc.balance(VALID_GTIN)
    orgs = out["organizations"]
    point = [o for o in orgs if o["inn"] == "7805825462"][0]
    assert point["error"] is not None
    # общий итог всё равно посчитан по 3 организациям
    assert out["total"]["EMITTED"]["km_count"] == 12 * 3


@pytest.mark.asyncio
async def test_exchange_token(svc):
    token = await svc.exchange_token("7805809291", "uuid-123", "sig-abc")
    assert token.startswith("mock-token-")
    assert svc.tokens.get("7805809291") == token


@pytest.mark.asyncio
async def test_exchange_token_bad_signature(svc):
    with pytest.raises(Exception):
        await svc.exchange_token("7805809291", "uuid-123", "BAD-signature")