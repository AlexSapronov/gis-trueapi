import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import main
from shipment_bulk import LEGACY_REQUIRED as REQUIRED, import_bulk
from warehouse_excel import make_xlsx, scan_data
from warehouse_service import check_document
from warehouse_store import WarehouseError, WarehouseStore
from test_warehouse import ORG, ORG2, GTIN, code, svc


@pytest.fixture
def store(tmp_path):
    return WarehouseStore(tmp_path / 'bulk.sqlite3')


def row(invoice=1, good=10, pn='PN', quantity=100, gtin=GTIN, flag='Да', org=ORG, recipient='1234567890', number=None):
    return [invoice, good, pn, quantity, number or f'INV-{invoice}', gtin, flag, org, recipient]


def excel(*rows):
    # Extra export columns are ignored and columns may be reordered.
    return make_xlsx({'Лист1': [REQUIRED] + list(rows)})


def upload(store, *rows):
    return import_bulk(store, excel(*rows))


def test_repeat_completed_is_unchanged_and_restart(store, svc):
    first = upload(store, row())['results'][0]
    d = store.get(first['document_id'])
    assert d['kind'] == 'shipment' and d['status'] == 'active'
    assert d['recipient_inn'] == '1234567890'
    store.add_scan(d['id'], scan_data(code()))
    asyncio.run(check_document(store, svc, d['id']))
    completed = store.complete(d['id'])
    restarted = WarehouseStore(store.path)
    result = upload(restarted, row())['results'][0]
    assert result['status'] == 'unchanged'
    assert restarted.get(d['id']) == completed
    assert len(restarted.list(admin=True)) == 1


def test_changed_quantity_reopens_and_preserves_codes(store, svc):
    d = upload(store, row())['results'][0]['document_id']
    store.add_scan(d, scan_data(code()))
    asyncio.run(check_document(store, svc, d))
    before = store.complete(d)
    report = upload(store, row(quantity=200))
    after = store.get(d)
    assert report['counts']['updated'] == 1
    assert after['status'] == 'active' and not after['can_complete']
    assert after['scans'] == before['scans'] and after['result'] is None
    assert after['date'] == before['date'] and after['completed_at'] is None
    checked = asyncio.run(check_document(store, svc, d))
    assert not checked['can_complete']
    assert checked['result']['rows'][0]['remaining'] == 100


def test_removed_gtin_preserves_extra_scan_and_blocks_completion(store, svc):
    d = upload(store, row())['results'][0]['document_id']
    store.add_scan(d, scan_data(code()))
    upload(store, row(gtin='', flag='??'))
    checked = asyncio.run(check_document(store, svc, d))
    assert not checked['can_complete']
    assert 'Лишний GTIN' in ' '.join(checked['result']['codes'][0]['errors'])


def test_unknown_is_unmarked_and_unmarked_only_not_created(store):
    report = upload(store, row(), row(good=11, gtin='', flag='??', pn='Other'),
                    row(invoice=2, gtin='', flag='Нет'))
    assert report['counts']['created'] == 1 and report['counts']['skipped'] == 1
    d = store.get(report['results'][0]['document_id'])
    assert len(d['lines']) == 2 and sum(not l['gtin'] for l in d['lines']) == 1


def test_bad_row_blocks_whole_invoice_but_other_invoices_import(store):
    report = upload(store, row(), row(good=11, gtin='', flag='Да'), row(invoice=2))
    assert report['counts']['error'] == report['counts']['created'] == 1
    assert [d['number'] for d in store.list(admin=True)] == ['INV-2']


def test_unknown_sender_and_conflicting_recipients(store):
    report = upload(store, row(org='1234567890'), row(invoice=2),
                    row(invoice=2, good=11, recipient='2222222222'))
    assert report['counts']['error'] == 2
    assert not store.list(admin=True)


def test_multi_org_and_parallel_repeat(store):
    content = excel(row(), row(invoice=2, org=ORG2))
    with ThreadPoolExecutor(2) as pool:
        reports = list(pool.map(lambda _: import_bulk(store, content), range(2)))
    assert sum(r['counts']['created'] for r in reports) == 2
    assert sum(r['counts']['unchanged'] for r in reports) == 2
    assert len(store.list(admin=True)) == 2


def test_absent_invoice_not_deleted(store):
    upload(store, row(), row(invoice=2))
    upload(store, row(invoice=2))
    assert len(store.list(admin=True)) == 2


def test_existing_manual_adopted_and_ambiguous_rejected(store):
    header = dict(kind='shipment', number='INV-1', date='2026-01-01', org_inn=ORG, org_name='Org', recipient_inn='1234567890')
    lines = [dict(gtin=GTIN, pn='PN', pn_source='excel', quantity=100)]
    d = store.create(header, lines, [])
    assert upload(store, row())['results'][0]['status'] == 'unchanged'
    assert store.get(d['id'])['date'] == '2026-01-01'
    header['number'] = 'INV-2'
    store.create(header, lines, [])
    store.create(header, lines, [])
    assert upload(store, row(invoice=2))['counts']['error'] == 1


def test_archive_deleted_and_checking_conflicts(store):
    d = upload(store, row())['results'][0]['document_id']
    # A running check cannot race with plan replacement.
    store.begin_check(d)
    assert upload(store, row(quantity=200))['counts']['error'] == 1
    with store.connection(True) as c:
        c.execute('UPDATE warehouse_documents SET check_id=NULL,check_until=NULL,archived_at=? WHERE id=?', ('2026-01-01', d))
    assert upload(store, row())['counts']['unchanged'] == 1
    assert upload(store, row(quantity=200))['counts']['error'] == 1
    with store.connection(True) as c:
        c.execute('UPDATE warehouse_documents SET deleted_at=? WHERE id=?', ('2026-01-01', d))
    assert upload(store, row())['counts']['error'] == 1


def test_http_auth_and_endpoint(store, monkeypatch):
    monkeypatch.setattr(main, 'warehouse', store)
    monkeypatch.setattr(main.settings, 'admin_key', 'bulk-test')
    client = TestClient(main.app)
    path = '/api/admin/warehouse/shipment-invoices'
    files = {'file': ('invoices.xlsx', excel(row()))}
    assert client.post(path, files=files).status_code == 403
    result = client.post(path, files=files, headers={'X-Admin-Key': 'bulk-test'})
    assert result.status_code == 200 and result.json()['counts']['created'] == 1
    assert client.post(path, files={'file': ('bad.xlsx', b'bad')}, headers={'X-Admin-Key': 'bulk-test'}).status_code == 422


def test_reject_missing_headers_and_unidentifiable_row(store):
    with pytest.raises(WarehouseError):
        import_bulk(store, make_xlsx({'Лист1': [['InvoiceId'], [1]]}))
    with pytest.raises(WarehouseError):
        upload(store, row(), row(invoice='bad'))
    assert not store.list(admin=True)


def test_unknown_with_gtin_still_unmarked(store):
    result = upload(store, row(), row(good=11, pn='Unknown', flag='??'))
    d = store.get(result['results'][0]['document_id'])
    assert next(l for l in d['lines'] if l['pn'] == 'Unknown')['gtin'] == ''


def test_recipient_rename_and_new_gtin_require_fresh_check(store):
    d = upload(store, row())['results'][0]['document_id']
    store.add_scan(d, scan_data(code()))
    second_gtin = '04640638349360'
    result = upload(store, row(number='RENAMED', recipient='2222222222'),
                    row(good=11, pn='New', gtin=second_gtin, number='RENAMED', recipient='2222222222'))
    after = store.get(d)
    assert result['counts']['updated'] == 1
    assert after['number'] == 'RENAMED' and after['recipient_inn'] == '2222222222'
    assert len(after['lines']) == 2 and len(after['scans']) == 1
    assert not after['check_current'] and not after['can_complete']
