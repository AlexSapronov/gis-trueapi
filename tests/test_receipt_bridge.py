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


@pytest.mark.parametrize("slave_state", ["active", "completed", "archived", "deleted", "checking"])
def test_master_revision_replaces_active_receipt_and_preserves_scans_in_history(store, slave_state):
    first = store.sync_receipt(payload())
    store.start(first["document_id"])
    store.add_scan(first["document_id"], scan_data(code()))
    doc_id = first["document_id"]
    if slave_state == "checking":
        store.begin_check(doc_id)
    with store.connection(True) as c:
        if slave_state == "completed":
            c.execute("UPDATE warehouse_documents SET status='completed' WHERE id=?", (doc_id,))
        elif slave_state == "archived":
            c.execute("UPDATE warehouse_documents SET archived_at='2026-11-13' WHERE id=?", (doc_id,))
        elif slave_state == "deleted":
            c.execute("UPDATE warehouse_documents SET deleted_at='2026-11-13' WHERE id=?", (doc_id,))
    assert store.sync_receipt(payload())["status"] == "unchanged"
    assert store.sync_receipt(payload(2, number="changed"))["status"] == "updated"
    doc = store.get(first["document_id"])
    assert doc["number"] == "changed" and doc["scans"] == [] and doc["status"] == "draft"
    assert doc["receipt_source"]["revision"] == 2
    with store.connection() as c:
        event = c.execute("SELECT details FROM warehouse_events WHERE action='receipt_superseded'").fetchone()
    import json
    assert len(json.loads(event["details"])["scans"]) == 1


def test_new_master_revision_restores_deleted_receipt(store):
    first = store.sync_receipt(payload())
    doc = store.get(first["document_id"])
    store.delete(doc["id"], doc["version"], doc["revision"])
    assert store.sync_receipt(payload())["status"] == "unchanged"
    assert len(store.list(admin=True, kind="receipt", trash=True)) == 1
    assert store.sync_receipt(payload(2))["status"] == "updated"
    assert store.get(doc["id"])["deleted_at"] is None


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


def manual_receipt(store, body=None, number="INV/1"):
    from receipt_bridge import prepare

    body = body or payload()
    org, lines, expected = prepare(body)
    return store.create(
        {
            "kind": "receipt",
            "number": number,
            "date": "2026-10-01",
            "org_inn": org.inn,
            "org_name": org.name,
        },
        lines,
        expected,
    )


@pytest.mark.parametrize("status", ["draft", "active", "completed", "archived"])
def test_existing_receipt_adopted_without_changing_composition_scans_or_check(
    store, status
):
    doc = manual_receipt(store)
    with store.connection(True) as c:
        c.execute(
            "UPDATE warehouse_documents SET status=?,archived_at=? WHERE id=?",
            (
                "completed" if status == "archived" else status,
                "2026-11-12" if status == "archived" else None,
                doc["id"],
            ),
        )
    if status == "active":
        store.add_scan(doc["id"], scan_data(code()))
    before = store.get(doc["id"])
    result = store.sync_receipt(payload())
    after = store.get(doc["id"])
    assert result["status"] == "matched" and result["document_id"] == doc["id"]
    assert all(
        after[k] == before[k]
        for k in (
            "status",
            "date",
            "number",
            "scans",
            "expected",
            "lines",
            "version",
            "revision",
            "result",
            "archived_at",
        )
    )
    assert store.sync_receipt(payload())["document_id"] == doc["id"]
    with store.connection() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM warehouse_documents WHERE kind='receipt'"
            ).fetchone()[0]
            == 1
        )


def test_full_code_match_finds_existing_receipt_with_different_number(store):
    doc = manual_receipt(store, number="supplier-export-123")
    assert store.sync_receipt(payload())["document_id"] == doc["id"]
    assert store.get(doc["id"])["number"] == "supplier-export-123"


@pytest.mark.parametrize(
    "mismatch",
    ["quantity", "pn", "crypto", "partial", "duplicate", "different_source"],
)
def test_conflicting_existing_receipt_blocks_duplicate_without_mutation(
    store, mismatch
):
    original = payload().model_dump(mode="json")
    if mismatch == "quantity":
        original["codes"][0]["quantity"] = 51
    if mismatch == "pn":
        original["codes"][0]["pn"] = "OTHER"
    if mismatch == "crypto":
        original["codes"][0]["raw"] = code(tail="91EE12\x1d92OTHERcrypto+/=")
    if mismatch == "partial":
        original["codes"].append(
            {"raw": code("OtherCode"), "pn": "PN-1", "quantity": 1, "atk": "0" * 25}
        )
    doc = manual_receipt(store, ReceiptPayload.model_validate(original))
    if mismatch == "duplicate":
        manual_receipt(store)
    if mismatch == "different_source":
        store.sync_receipt(payload(source_document="another-invoice"))
    with store.connection() as c:
        before = [tuple(r) for r in c.execute("SELECT * FROM warehouse_documents")]
    with pytest.raises(WarehouseError) as err:
        store.sync_receipt(payload())
    assert err.value.code == "receipt_match_conflict"
    with store.connection() as c:
        assert [
            tuple(r) for r in c.execute("SELECT * FROM warehouse_documents")
        ] == before


def test_parallel_manual_adoption_links_once(store):
    doc = manual_receipt(store)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: store.sync_receipt(payload()), range(4)))
    assert {r["document_id"] for r in results} == {doc["id"]}
    assert sum(r["status"] == "matched" for r in results) == 1


def revoke_payload(revision=2):
    from receipt_bridge import RevokePayload
    return RevokePayload(source_instance='bm-test', source_document='invoice-1', revision=revision, reason='Исправление груза', action='revoke')


def test_revoke_hides_old_receipt_rejects_stale_delivery_and_accepts_new_composition(store):
    original = store.sync_receipt(payload())
    result = store.revoke_receipt(revoke_payload())
    assert result['status'] == 'revoked'
    assert store.revoke_receipt(revoke_payload()) == result
    with pytest.raises(WarehouseError):
        store.get(original['document_id'])
    with pytest.raises(WarehouseError):
        store.sync_receipt(payload())
    with pytest.raises(WarehouseError):
        store.start(original['document_id'])
    new = store.sync_receipt(payload(3, number='CORRECTED'))
    assert new['document_id'] == original['document_id']
    assert store.get(new['document_id'])['number'] == 'CORRECTED'
    with pytest.raises(WarehouseError):
        store.revoke_receipt(revoke_payload())


def test_revoke_before_delivery_leaves_tombstone_and_does_not_create_old_receipt(store):
    store.revoke_receipt(revoke_payload())
    with pytest.raises(WarehouseError):
        store.sync_receipt(payload())
    assert store.sync_receipt(payload(3))['status'] == 'created'


@pytest.mark.parametrize("status", ["active", "completed", "archived", "deleted", "checking"])
def test_master_revoke_overrides_any_slave_state_and_old_check(store, status):
    import json
    original = store.sync_receipt(payload())
    doc_id = original["document_id"]
    store.start(doc_id)
    store.add_scan(doc_id, scan_data(code()))
    check_id, checked = store.begin_check(doc_id)
    with store.connection(True) as c:
        if status == "completed":
            c.execute("UPDATE warehouse_documents SET status='completed' WHERE id=?", (doc_id,))
        elif status == "archived":
            c.execute("UPDATE warehouse_documents SET archived_at='2026-11-13' WHERE id=?", (doc_id,))
        elif status == "deleted":
            c.execute("UPDATE warehouse_documents SET deleted_at='2026-11-13' WHERE id=?", (doc_id,))
    assert store.revoke_receipt(revoke_payload())["status"] == "revoked"
    with pytest.raises(WarehouseError):
        store.start(doc_id)
    with store.connection() as c:
        event = c.execute("SELECT details FROM warehouse_events WHERE action='receipt_revoked'").fetchone()
    assert len(json.loads(event["details"])["scans"]) == 1
    store.sync_receipt(payload(3))
    # A check started against the former version cannot publish its result.
    store.finish_check(doc_id, check_id, checked["version"], {"ok": True})
    doc = store.get(doc_id)
    assert doc["status"] == "draft" and doc["scans"] == []
    assert doc["result"] is None and not doc["checking"]


def test_revoke_route_requires_bridge_key_and_returns_exact_revision(store, monkeypatch):
    monkeypatch.setattr(main, 'warehouse', store)
    monkeypatch.setattr(settings, 'receipt_bridge_key', 'k'*48)
    c = TestClient(main.app)
    url = '/api/integrations/marking-bm/receipts/revoke'
    body = revoke_payload().model_dump(mode='json')
    assert c.post(url, json=body).status_code == 403
    r = c.post(url, json=body, headers={'X-Receipt-Bridge-Key':'k'*48})
    assert r.status_code == 200, r.text
    assert r.json()['revision'] == 2 and r.json()['status'] == 'revoked'
    body['revision'] = True
    assert c.post(url, json=body, headers={'X-Receipt-Bridge-Key':'k'*48}).status_code == 422


def test_revoke_matches_old_manual_receipt_by_exact_composition(store):
    from receipt_bridge import RevokePayload
    original = payload()
    doc = manual_receipt(store, original)['id']
    request = revoke_payload().model_dump()
    request['receipt'] = original.model_dump()
    result = store.revoke_receipt(RevokePayload.model_validate(request))
    assert result['document_id'] == doc
    with pytest.raises(WarehouseError):
        store.get(doc)
