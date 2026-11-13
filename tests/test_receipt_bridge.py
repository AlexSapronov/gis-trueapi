from concurrent.futures import ThreadPoolExecutor

import main
import pytest
from config import settings
from fastapi.testclient import TestClient
from receipt_bridge import ReceiptPayload
from test_warehouse import code
from warehouse_excel import scan_data
from warehouse_store import WarehouseError, WarehouseStore


def payload(revision=1, **changes):
    body = {
        "source_instance": "bm-test",
        "source_document": "invoice-1",
        "revision": revision,
        "number": "INV/1",
        "date": "2026-11-13",
        "org_inn": "0000000001",
        "supplier": "Supplier",
        "codes": [
            {"raw": code(), "pn": "PN-1", "quantity": 50, "atk": "0" * 20 + "AbCd1"}
        ],
    }
    body.update(changes)
    return ReceiptPayload.model_validate(body)


@pytest.fixture
def store(tmp_path):
    return WarehouseStore(tmp_path / "wh.sqlite3")


def test_retries_restart_and_concurrent_delivery_create_one_hidden_receipt(store):
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: store.sync_receipt(payload()), range(4)))
    assert len({r["document_id"] for r in results}) == 1
    assert sum(r["status"] == "created" for r in results) == 1
    other = WarehouseStore(store.path)
    doc = other.get(results[0]["document_id"])
    assert doc["status"] == "draft" and not other.list(kind="receipt")
    assert len(doc["expected"]) == 1 and doc["lines"][0]["quantity"] == 50
    assert doc["receipt_source"]["atks"] == ["0" * 20 + "AbCd1"]
    assert other.sync_receipt(payload())["status"] == "unchanged"


def test_draft_updates_but_old_and_conflicting_revisions_are_rejected(store):
    first = store.sync_receipt(payload())
    second = payload(
        2,
        codes=[
            {
                "raw": code("DifferentCode"),
                "pn": "PN-1",
                "quantity": 20,
                "atk": "0" * 20 + "AbCd1",
            }
        ],
    )
    assert store.sync_receipt(second)["document_id"] == first["document_id"]
    doc = store.get(first["document_id"])
    assert doc["lines"][0]["quantity"] == 20
    assert len(doc["expected"]) == 1 and "DifferentCode" in doc["expected"][0]["cis"]
    for bad in (payload(), payload(2)):
        with pytest.raises(WarehouseError, match="верси"):
            store.sync_receipt(bad)


def test_active_receipt_retries_acknowledge_but_changes_preserve_scans(store):
    first = store.sync_receipt(payload())
    store.start(first["document_id"])
    store.add_scan(first["document_id"], scan_data(code()))
    assert store.sync_receipt(payload())["status"] == "unchanged"
    with pytest.raises(WarehouseError, match="запущена"):
        store.sync_receipt(payload(2, number="changed"))
    doc = store.get(first["document_id"])
    assert doc["number"] == "INV/1" and len(doc["scans"]) == 1
    assert doc["receipt_source"]["revision"] == 1


def test_deleted_receipt_is_not_recreated(store):
    first = store.sync_receipt(payload())
    doc = store.get(first["document_id"])
    store.delete(doc["id"], doc["version"], doc["revision"])
    with pytest.raises(WarehouseError, match="корзине"):
        store.sync_receipt(payload())
    assert len(store.list(admin=True, kind="receipt", trash=True)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"org_inn": "9999999999"},
        {"codes": [{"raw": code(), "pn": "PN", "quantity": 1, "atk": "0" * 25}] * 2},
        {
            "codes": [
                {
                    "raw": code().split("\x1d")[0],
                    "pn": "PN",
                    "quantity": 1,
                    "atk": "0" * 25,
                }
            ]
        },
    ],
)
def test_bad_composition_never_creates_partial_document(store, changes):
    with pytest.raises(WarehouseError):
        store.sync_receipt(payload(**changes))
    assert not store.list(admin=True, kind="receipt")


def test_ingress_uses_only_dedicated_key_and_does_not_echo_codes(store, monkeypatch):
    monkeypatch.setattr(main, "warehouse", store)
    monkeypatch.setattr(settings, "receipt_bridge_key", "k" * 48)
    c = TestClient(main.app)
    url = "/api/integrations/marking-bm/receipts"
    body = payload().model_dump(mode="json")
    assert c.post(url, json=body).status_code == 403
    assert (
        c.post(url, json=body, headers={"X-Admin-Key": settings.admin_key}).status_code
        == 403
    )
    headers = {"X-Receipt-Bridge-Key": "k" * 48}
    assert c.post(url, json=body, headers=headers).status_code == 200
    body["codes"][0]["quantity"] = True
    response = c.post(url, json=body, headers=headers)
    assert response.status_code == 422 and code() not in response.text
    monkeypatch.setattr(settings, "receipt_bridge_key", "")
    assert c.post(url, json=body, headers=headers).status_code == 403
