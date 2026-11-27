import asyncio
import io
from concurrent.futures import ThreadPoolExecutor

import openpyxl
import pytest
from fastapi.testclient import TestClient

import main
from shipment_bulk import REQUIRED, import_bulk
from warehouse_excel import make_xlsx, scan_data
from warehouse_service import check_document
from warehouse_store import WarehouseError, WarehouseStore
from test_warehouse import ORG, ORG2, GTIN, code, svc, workbook


@pytest.fixture
def store(tmp_path):
    return WarehouseStore(tmp_path / 'bulk.sqlite3')


def row(invoice=1, good=10, pn='PN', quantity=100, gtin=GTIN, flag='Да', org=ORG, recipient='1234567890', number=None):
    return [invoice, good, pn, quantity, number or f'INV-{invoice}', gtin, flag, org, recipient]


def excel(*rows):
    # Extra export columns are ignored and columns may be reordered.
    return make_xlsx({'Лист1': [REQUIRED] + list(rows)})


def flarp_excel(*rows, recipient_name='Получатель из Фларпа'):
    # The actual export places both names before the two explicit INN columns.
    headers = (*REQUIRED[:-2], 'Отправитель', 'Получатель', *REQUIRED[-2:], 'Кол-во КМ к заказу')
    return make_xlsx({'Лист1': [headers, *[
        [*values[:-2], 'Отправитель из Фларпа', recipient_name, *values[-2:], None] for values in rows
    ]]})


def gtd_row(values, amount=None, declaration=None):
    invoice, good, pn, quantity, number, gtin, flag, org, recipient = values
    return [number or f'INV-{invoice}', good, quantity if amount is None else amount,
            declaration or f'DECL-{invoice}-{good}', org, recipient]


def gtd_book(*rows):
    return make_xlsx({'ГТД': [['Number', 'GoodId', 'Amount', 'Declaration', 'FromINN', 'ToINN'], *rows]})


def upload(store, *rows):
    return import_bulk(store, excel(*rows), gtd_book(*(gtd_row(item) for item in rows)))


def test_bulk_task_waits_for_gtd_when_optional_file_is_not_uploaded(store, svc):
    result = import_bulk(store, excel(row()))['results'][0]
    d = store.get(result['document_id'])
    assert d['gtd_required'] and d['gtd_state'] == 'pending'
    store.add_scan(d['id'], scan_data(code()))
    checked = asyncio.run(check_document(store, svc, d['id']))
    assert not checked['can_complete']
    assert checked['result']['gtd']['state'] == 'pending'
    assert 'Груз пока не упаковывать' in checked['result']['gtd']['message']
    with pytest.raises(WarehouseError):
        store.complete(d['id'])


def test_gtd_workbook_quantity_must_match_bulk_goodid(store):
    report = import_bulk(store, excel(row(quantity=100)), gtd_book(gtd_row(row(quantity=100), amount=99)))
    d = store.get(report['results'][0]['document_id'])
    assert d['gtd_state'] == 'invalid'
    assert '99 шт.' in d['gtd_message'] and '100 шт.' in d['gtd_message']


def test_gtd_fifo_identifies_a_code_crossing_two_declaration_rows():
    from warehouse_service import reconcile_gtd_fifo
    doc = {'gtd_required': True, 'gtd_state': 'ready', 'gtd_rows': [
        {'position':1,'gtin':GTIN,'quantity':300,'declaration':'D1'},
        {'position':2,'gtin':GTIN,'quantity':100,'declaration':'D2'},
    ]}
    result = reconcile_gtd_fifo(doc, [
        {'scan_id':'scan-1','cis':'CI-1','gtin':GTIN,'quantity':400,'errors':[]},
    ])
    assert not result['ok'] and len(result['split_required']) == 1
    assert result['split_required'][0]['parts'] == [300]
    assert result['split_required'][0]['remaining_quantity'] == 100
    assert result['split_required'][0]['declarations'] == ['D1','D2']


@pytest.mark.asyncio
async def test_auto_split_requires_rescan_and_successful_divisionmark(store, svc):
    values = row(quantity=400)
    report = import_bulk(store, excel(values), gtd_book(
        gtd_row(values, amount=300, declaration='D1'),
        gtd_row(values, amount=100, declaration='D2')))
    document_id = report['results'][0]['document_id']
    original_code = code('INTRODUCEDQ400')
    updated_source = original_code + '\x1d30' + '100'
    new_code = code('INTRODUCEDNEWQ300') + '\x1d30' + '300'
    source_cis = scan_data(updated_source)['cis']
    new_cis = scan_data(new_code)['cis']

    class SplitClient:
        enabled = True
        confirmed = False
        async def create(self, code_value, parts, request_id):
            assert parts == [300]
            return {'id':'batch-1','state':'ordering','expected_codes':[]}
        async def status(self, batch_id):
            if self.confirmed:
                state = 'done'
            else:
                state = 'ready_to_rescan'
            return {'id':batch_id,'state':state,'error':None,
                    'expected_codes':[
                        {'cis':new_cis,'quantity':300,'role':'new'},
                        {'cis':source_cis,'quantity':100,'role':'source'}]}
        async def confirm(self, batch_id, scanned_cis):
            assert set(scanned_cis) == {source_cis,new_cis}
            self.confirmed = True
            return {'id':batch_id,'state':'reporting','error':None,
                    'expected_codes':[
                        {'cis':new_cis,'quantity':300,'role':'new'},
                        {'cis':source_cis,'quantity':100,'role':'source'}]}
        async def labels(self, batch_id):
            return b'%PDF-test'

    split = SplitClient()
    svc.km_split_client = split
    original_info = svc.client.info
    async def info(cises, token):
        results = await original_info(cises, token)
        for item in results:
            cis = (item.get('cisInfo') or {}).get('cis')
            if split.confirmed:
                if cis == source_cis:
                    item['cisInfo']['quantityInPack'] = 100
                elif cis == new_cis:
                    item['cisInfo']['quantityInPack'] = 300
        return results
    svc.client.info = info

    store.add_scan(document_id, scan_data(original_code))
    started = await check_document(store, svc, document_id)
    assert not started['can_complete']
    assert started['scans'] == []
    assert started['splits'][0]['state'] == 'ordering'

    store.add_scan(document_id, scan_data(updated_source))
    store.add_scan(document_id, scan_data(new_code))
    awaiting = await check_document(store, svc, document_id)
    assert awaiting['result']['gtd']['ok'], awaiting['result']['gtd']
    assert awaiting['splits'][0]['state'] == 'reporting'
    assert not awaiting['can_complete']

    completed = await check_document(store, svc, document_id)
    assert completed['splits'][0]['state'] == 'done'
    assert completed['result']['gtd']['ok']
    assert completed['can_complete']


def test_split_removes_active_scan_but_keeps_audit_and_is_idempotent(store):
    values = row(quantity=400)
    report = import_bulk(store, excel(values), gtd_book(
        gtd_row(values, amount=300, declaration='D1'),
        gtd_row(values, amount=100, declaration='D2')))
    document_id = report['results'][0]['document_id']
    store.add_scan(document_id, scan_data(code()))
    scan_id = store.get(document_id)['scans'][0]['id']
    request_id = 'gtd-split-request-1'
    remote = {'id':'remote-batch-1','state':'ordering','expected_codes':[]}
    first = store.create_split(document_id, scan_id, request_id, [300], remote)
    assert not first['scans']
    assert first['splits'][0]['state'] == 'ordering'
    assert first['splits'][0]['source_cis']
    store.create_split(document_id, scan_id, request_id, [300], remote)
    assert len(store.get(document_id)['splits']) == 1
    with store.connection() as c:
        events = [row['action'] for row in c.execute(
            'SELECT action FROM warehouse_events WHERE document_id=? ORDER BY id', (document_id,))]
    assert events.count('scan_removed_for_gtd_split') == 1


def test_gtd_fifo_uses_original_scan_position_and_split_part_order():
    from warehouse_service import reconcile_gtd_fifo
    source = {'cis':'SOURCE-CIS','quantity':100,'role':'source'}
    new = {'cis':'NEW-CIS','quantity':300,'role':'new'}
    doc = {'gtd_required':True,'gtd_state':'ready','gtd_rows':[
        {'position':1,'gtin':GTIN,'quantity':300,'declaration':'D1'},
        {'position':2,'gtin':GTIN,'quantity':100,'declaration':'D2'},
    ],'splits':[{'source_scanned_at':'2026-11-27T08:00:00+00:00',
                 'created_at':'2026-11-27T08:02:00+00:00',
                 'expected_codes':[new,source]}]}
    # The retained/source label was scanned first by the operator, but FIFO
    # placement follows the original scan's position and the generated part order.
    result = reconcile_gtd_fifo(doc, [
        {'scan_id':'source','cis':'SOURCE-CIS','gtin':GTIN,'quantity':100,'errors':[],
         'scanned_at':'2026-11-27T08:03:00+00:00'},
        {'scan_id':'new','cis':'NEW-CIS','gtin':GTIN,'quantity':300,'errors':[],
         'scanned_at':'2026-11-27T08:04:00+00:00'},
    ])
    assert result['ok'] and not result['split_required']


def test_gtd_fifo_accepts_codes_aligned_to_declaration_boundaries():
    from warehouse_service import reconcile_gtd_fifo
    doc = {'gtd_required': True, 'gtd_state': 'ready', 'gtd_rows': [
        {'position':1,'gtin':GTIN,'quantity':300,'declaration':'D1'},
        {'position':2,'gtin':GTIN,'quantity':100,'declaration':'D2'},
    ]}
    result = reconcile_gtd_fifo(doc, [
        {'scan_id':'scan-1','cis':'CI-1','gtin':GTIN,'quantity':300,'errors':[]},
        {'scan_id':'scan-2','cis':'CI-2','gtin':GTIN,'quantity':100,'errors':[]},
    ])
    assert result['ok'] and not result['split_required']
    assert result['message'] == 'ГТД распределены по физическим КМ. Можно паковать.'


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

    updated_name = import_bulk(restarted, flarp_excel(row()))['results'][0]
    assert updated_name['status'] == 'unchanged'
    renamed = restarted.get(d['id'])
    assert renamed['recipient_name'] == 'Получатель из Фларпа'
    assert renamed['status'] == 'completed' and renamed['check_current']
    assert renamed['scans'] == completed['scans']


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
    assert upload(store, row())['results'][0]['status'] == 'updated'
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
    files = {'file': ('invoices.xlsx', flarp_excel(row()))}
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



@pytest.mark.parametrize('identifier', [123456789, '123456789012', 'BY-001234567', '001234567'])
def test_flarp_export_uses_explicit_inns_for_same_named_recipients(store, identifier):
    content = flarp_excel(row(recipient=identifier), row(invoice=2, recipient='2222222222'))
    result = import_bulk(store, content)
    assert result['counts']['created'] == 2
    docs = [store.get(entry['document_id']) for entry in result['results']]
    assert [d['recipient_inn'] for d in docs] == [str(identifier), '2222222222']
    assert all(d['recipient_name'] == 'Получатель из Фларпа' for d in docs)
    assert all(d['status'] == 'active' for d in docs)


@pytest.mark.parametrize('value', [None, '', '   ', True, 12.5])
def test_missing_or_invalid_explicit_inn_is_reported(store, value):
    content = flarp_excel(row(recipient=value))
    result = import_bulk(store, content)['results'][0]
    assert result['status'] == 'error' and 'Строка 2: ИНН получателя' in result['message']
    assert not store.list(admin=True)


def test_name_only_export_requires_the_new_inn_columns(store):
    headers = (*REQUIRED[:-2], 'Отправитель', 'Получатель')
    with pytest.raises(WarehouseError, match='ИНН отправителя, ИНН получателя'):
        import_bulk(store, make_xlsx({'Лист1': [headers, row()]}))


def test_reimport_releases_legacy_draft_with_existing_scans(store):
    doc_id = upload(store, row())['results'][0]['document_id']
    store.add_scan(doc_id, scan_data(code()))
    before = store.get(doc_id)
    with store.connection(True) as c:
        c.execute("ALTER TABLE warehouse_documents ADD COLUMN recipient_choice_key TEXT NOT NULL DEFAULT ''")
        c.execute("""UPDATE warehouse_documents
            SET recipient_choice_key='legacy',recipient_inn='',status='draft' WHERE id=?""", (doc_id,))
    reopened = WarehouseStore(store.path)
    assert reopened.get(doc_id)['awaiting_invoice_inn'] and not reopened.list()
    with pytest.raises(WarehouseError, match='Повторно загрузите'):
        reopened.start(doc_id)

    result = import_bulk(reopened, flarp_excel(row()))['results'][0]
    assert result['status'] == 'updated' and result['document_id'] == doc_id
    after = reopened.list()[0]
    assert after['recipient_inn'] == '1234567890' and not after['awaiting_invoice_inn']
    assert reopened.get(doc_id)['scans'] == before['scans']


def test_standard_shipment_accepts_foreign_client_identifier():
    from warehouse_excel import import_xlsx
    book = openpyxl.load_workbook(io.BytesIO(workbook()))
    book['Документ']['B6'] = '123456789'
    stream = io.BytesIO()
    book.save(stream)
    header, _, _ = import_xlsx(stream.getvalue(), 'shipment')
    assert header['recipient_inn'] == '123456789'
