import asyncio
import io
import sqlite3

import openpyxl
import pytest
from fastapi.testclient import TestClient

import main
from party_catalog import name_key
from shipment_bulk import REQUIRED, import_bulk, read_party_catalog
from warehouse_excel import make_xlsx, scan_data
from warehouse_service import check_document
from warehouse_store import WarehouseError, WarehouseStore
from test_shipment_bulk import store, row
from test_warehouse import ORG, GTIN, code, svc

CLIENT = '1234567890'
SENDER_NAME = 'ООО «Наш поставщик»'
CLIENT_NAME = 'ООО «Клиент»'


def catalog(*rows):
    return make_xlsx({'Юрлица': [('ИНН', 'Имя'), *rows]})


def setup_catalog(store):
    return store.save_party_catalog(read_party_catalog(catalog((ORG, SENDER_NAME), (CLIENT, CLIENT_NAME))))


def invoice(*rows):
    return make_xlsx({'Лист1': [REQUIRED, *rows]})


def named_row(**kwargs):
    return row(org=SENDER_NAME, recipient=CLIENT_NAME, **kwargs)


def test_persistent_catalog_and_named_invoice(store):
    assert setup_catalog(store) == {'added': 2, 'existing': 0, 'total': 2}
    restarted = WarehouseStore(store.path)
    result = import_bulk(restarted, invoice(named_row()))
    assert result['counts']['created'] == 1
    d = restarted.get(result['results'][0]['document_id'])
    assert d['org_inn'] == ORG and d['recipient_inn'] == CLIENT
    assert d['recipient_name'] == CLIENT_NAME
    assert import_bulk(restarted, invoice(named_row()))['counts']['unchanged'] == 1


def test_names_normalize_case_whitespace_and_quotes(store):
    setup_catalog(store)
    values = named_row()
    values[-2] = '  ооо  "Наш поставщик"  '
    values[-1] = 'ооо\u00a0“Клиент”'
    assert import_bulk(store, invoice(values))['counts']['created'] == 1
    assert name_key('ООО «Клиент»') == name_key('ооо "клиент"')


def test_unknown_name_does_not_partially_change_invoice(store):
    setup_catalog(store)
    good = named_row(invoice=2)
    bad = named_row(good=11)
    bad[-1] = 'Неизвестное юрлицо'
    result = import_bulk(store, invoice(named_row(), bad, good))
    assert result['counts']['error'] == 1 and result['counts']['created'] == 1
    assert 'строка 3' in result['results'][0]['message'].casefold()
    assert [d['number'] for d in store.list(admin=True)] == ['INV-2']


def test_catalog_supports_multiple_inns_per_name(store):
    setup_catalog(store)
    rows = read_party_catalog(catalog(
        ('3333333333', 'ООО «Дубль»'), ('4444444444', 'ооо "дубль"'),
        ('4444444444', 'ООО «Дубль»'), ('4444444444', CLIENT_NAME)))
    assert store.save_party_catalog(rows) == {'added': 3, 'existing': 0, 'total': 5}
    assert store.save_party_catalog(rows) == {'added': 0, 'existing': 3, 'total': 5}


def test_legacy_catalog_migrates_without_losing_existing_names(tmp_path):
    db = tmp_path / 'legacy.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE warehouse_party_catalog (name_key TEXT PRIMARY KEY, inn TEXT NOT NULL, name TEXT NOT NULL)')
        c.execute('INSERT INTO warehouse_party_catalog VALUES (?,?,?)', (name_key(CLIENT_NAME), CLIENT, CLIENT_NAME))
    store = WarehouseStore(db)
    assert store.party_catalog()['rows'] == [{'inn': CLIENT, 'name': CLIENT_NAME}]
    store.save_party_catalog([{'inn': '2222222222', 'name': CLIENT_NAME}])
    assert len(WarehouseStore(db).party_catalog()['rows']) == 2


def test_aliases_additive_and_reupload_idempotent(store):
    setup_catalog(store)
    assert setup_catalog(store)['existing'] == 2
    store.save_party_catalog(read_party_catalog(catalog((CLIENT, 'Клиент ООО'))))
    assert len(store.party_catalog()['rows']) == 3
    values = named_row()
    values[-1] = 'Клиент ООО'
    result = import_bulk(store, invoice(values))
    assert result['counts']['created'] == 1
    assert store.get(result['results'][0]['document_id'])['recipient_inn'] == CLIENT


def test_numeric_inn_and_bad_catalog(store):
    parsed = read_party_catalog(catalog((1234567890, 'Клиент')))
    assert parsed == [{'inn': CLIENT, 'name': 'Клиент'}]
    book = openpyxl.load_workbook(io.BytesIO(catalog((CLIENT, 'Клиент'))))
    book.active['B2'] = '=1+1'
    stream = io.BytesIO()
    book.save(stream)
    for data in (catalog(('', 'Клиент')), stream.getvalue(), catalog((CLIENT, None))):
        with pytest.raises(WarehouseError, match='строка 2'):
            read_party_catalog(data)
    with pytest.raises(WarehouseError):
        store.save_party_catalog(read_party_catalog(catalog()))


def test_sender_must_still_be_our_organization(store):
    setup_catalog(store)
    values = named_row()
    values[-2] = CLIENT_NAME
    result = import_bulk(store, invoice(values))
    assert result['counts']['error'] == 1 and not store.list(admin=True)


def test_legacy_to_names_preserves_completed_scans(store, svc):
    from test_shipment_bulk import excel
    setup_catalog(store)
    result = import_bulk(store, excel(row()))
    doc_id = result['results'][0]['document_id']
    store.add_scan(doc_id, scan_data(code()))
    asyncio.run(check_document(store, svc, doc_id))
    before = store.complete(doc_id)
    result = import_bulk(store, invoice(named_row()))
    after = store.get(doc_id)
    assert result['counts']['unchanged'] == 1
    assert after['status'] == 'completed' and after['check_current']
    assert after['version'] == before['version']
    assert after['scans'] == before['scans'] and after['result'] == before['result']
    assert after['recipient_name'] == CLIENT_NAME


def test_changed_named_recipient_updates_inn_and_name(store):
    setup_catalog(store)
    doc_id = import_bulk(store, invoice(named_row()))['results'][0]['document_id']
    store.save_party_catalog([{'inn': '2222222222', 'name': 'Другой клиент'}])
    values = named_row()
    values[-1] = 'Другой клиент'
    result = import_bulk(store, invoice(values))
    after = store.get(doc_id)
    assert result['counts']['updated'] == 1
    assert after['recipient_name'] == 'Другой клиент' and after['recipient_inn'] == '2222222222'
    assert not after['can_complete']


def test_catalog_endpoints_protected_and_template(store, monkeypatch):
    monkeypatch.setattr(main, 'warehouse', store)
    monkeypatch.setattr(main.settings, 'admin_key', 'party-test')
    client = TestClient(main.app)
    path = '/api/admin/warehouse/party-catalog'
    for suffix in ('', '/template'):
        assert client.get(path + suffix).status_code == 403
    assert client.post(path, files={'file': ('c.xlsx', catalog((CLIENT, CLIENT_NAME)))}).status_code == 403
    assert client.delete(path).status_code == 403
    headers = {'X-Admin-Key': 'party-test'}
    response = client.get(path + '/template', headers=headers)
    assert response.status_code == 200
    book = openpyxl.load_workbook(io.BytesIO(response.content))
    assert list(book.active.values) == [('ИНН', 'Имя')]
    response = client.post(path, headers=headers, files={'file': ('c.xlsx', catalog((CLIENT, CLIENT_NAME)))})
    assert response.status_code == 200 and response.json()['added'] == 1
    assert client.get(path, headers=headers).json()['rows'] == [{'inn': CLIENT, 'name': CLIENT_NAME}]
    assert client.post(path, headers=headers, data={'replace': 'true'}, files={'file': ('bad.xlsx', b'bad')}).status_code == 422
    assert client.get(path, headers=headers).json()['rows'] == [{'inn': CLIENT, 'name': CLIENT_NAME}]
    response = client.post(path, headers=headers, data={'replace': 'true'},
                           files={'file': ('fresh.xlsx', catalog((ORG, SENDER_NAME)))})
    assert response.json() == {'added': 1, 'existing': 0, 'total': 1}
    assert client.get(path, headers=headers).json()['rows'] == [{'inn': ORG, 'name': SENDER_NAME}]
    assert client.delete(path, headers=headers).json() == {'deleted': 1}
    assert client.get(path, headers=headers).json()['rows'] == []


def test_ambiguous_recipient_waits_for_admin_and_preserves_explicit_choice(store, monkeypatch):
    second = '2222222222'
    setup_catalog(store)
    store.save_party_catalog([{'inn': second, 'name': CLIENT_NAME}])
    monkeypatch.setattr(main, 'warehouse', store)
    monkeypatch.setattr(main.settings, 'admin_key', 'party-test')
    client = TestClient(main.app)
    headers = {'X-Admin-Key': 'party-test'}
    file = invoice(named_row())
    result = client.post('/api/admin/warehouse/shipment-invoices', headers=headers,
                         files={'file': ('invoice.xlsx', file)}).json()
    assert result['counts']['needs_recipient'] == 1
    doc_id = result['results'][0]['document_id']
    path = '/api/admin/warehouse/documents/' + doc_id
    worker_path = '/api/warehouse/documents/' + doc_id
    d = client.get(path, headers=headers).json()
    assert d['status'] == 'draft' and d['recipient_inn'] == ''
    assert d['recipient_options'] == [CLIENT, second]
    assert client.get('/api/warehouse/documents').json()['documents'] == []
    assert client.get(worker_path).status_code == 404
    assert client.post(worker_path + '/scans', json={'code': code()}).status_code == 404
    assert client.post(path + '/start', headers=headers).status_code == 409
    assert import_bulk(store, file)['results'][0]['document_id'] == doc_id

    choice = {'version': d['version'], 'revision': d['revision'], 'inn': second}
    assert client.post(path + '/recipient', json=choice).status_code == 403
    assert client.post(path + '/recipient', headers=headers, json={**choice, 'inn': ORG}).status_code == 409
    store.save_party_catalog([{'inn': ORG, 'name': SENDER_NAME}, {'inn': CLIENT, 'name': CLIENT_NAME}], replace=True)
    assert client.post(path + '/recipient', headers=headers, json=choice).status_code == 409
    assert import_bulk(store, file)['counts']['needs_recipient'] == 1
    store.save_party_catalog([{'inn': second, 'name': CLIENT_NAME}])
    selected = client.post(path + '/recipient', headers=headers, json=choice).json()
    assert selected['status'] == 'active' and selected['recipient_inn'] == second
    assert client.get(worker_path).status_code == 200
    assert client.post(worker_path + '/scans', json={'code': code()}).status_code == 200
    assert import_bulk(store, file)['counts']['unchanged'] == 1
    assert store.get(doc_id)['recipient_inn'] == second


def test_reimport_pauses_existing_shipment_when_recipient_becomes_ambiguous(store):
    setup_catalog(store)
    file = invoice(named_row())
    doc_id = import_bulk(store, file)['results'][0]['document_id']
    store.add_scan(doc_id, scan_data(code()))
    store.save_party_catalog([{'inn': '2222222222', 'name': CLIENT_NAME}])
    result = import_bulk(store, file)['results'][0]
    assert result['status'] == 'needs_recipient' and result['document_id'] == doc_id
    assert not store.list()
    d = store.get(doc_id)
    assert len(d['scans']) == 1
    store.clear_party_catalog()
    assert store.get(doc_id)['recipient_options'] == []
    with pytest.raises(WarehouseError, match='актуальном справочнике'):
        store.select_shipment_recipient(doc_id, CLIENT, d['version'], d['revision'])


@pytest.mark.parametrize('identifier', ['123456789', 123456789, '123456789012', 'BY-001234567', '001234567'])
def test_client_identifiers_without_russian_length_restriction(store, identifier):
    store.save_party_catalog(read_party_catalog(catalog((ORG, SENDER_NAME), (identifier, CLIENT_NAME))))
    result = import_bulk(store, invoice(named_row()))
    assert result['counts']['created'] == 1
    doc = store.get(result['results'][0]['document_id'])
    assert doc['recipient_inn'] == str(identifier)
    assert import_bulk(store, invoice(named_row()))['counts']['unchanged'] == 1
    from test_shipment_bulk import excel
    legacy = row(recipient=identifier)
    assert import_bulk(store, excel(legacy))['counts']['unchanged'] == 1


def test_standard_shipment_accepts_foreign_client_identifier():
    from test_warehouse import workbook
    from warehouse_excel import import_xlsx
    book = openpyxl.load_workbook(io.BytesIO(workbook()))
    book['Документ']['B6'] = '123456789'
    stream = io.BytesIO()
    book.save(stream)
    header, _, _ = import_xlsx(stream.getvalue(), 'shipment')
    assert header['recipient_inn'] == '123456789'


@pytest.mark.parametrize('value', [None, '', '   ', True, 12.5])
def test_empty_or_non_identifier_values_rejected(value):
    from party_catalog import inn_text
    with pytest.raises(ValueError):
        inn_text(value)
