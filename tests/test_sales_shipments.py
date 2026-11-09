from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import main
from config import Settings
from mockclient import MockTrueApiClient
from service import Service
from tokens import FileTokenStore
from warehouse_excel import make_xlsx
from warehouse_store import WarehouseStore

API = '/api/sales-shipments'
WRITE = {'X-Sales-Request': '1'}
ADMIN = {'X-Admin-Key': 'test-admin'}
ORG = '0000000001'
GTIN = '04640638345218'
KEYS = {'влад': 'A' * 43, 'полина': 'B' * 43}


@pytest.fixture
def store(monkeypatch, tmp_path):
    db = WarehouseStore(tmp_path / 'warehouse.sqlite3')
    tokens = FileTokenStore(tmp_path / 'tokens.json')
    tokens.set(ORG, 'mock-token')
    monkeypatch.setattr(main, 'warehouse', db)
    monkeypatch.setattr(main, 'svc', Service(MockTrueApiClient(), tokens))
    monkeypatch.setattr(main.settings, 'sales_shipment_links', KEYS)
    monkeypatch.setattr(main.settings, 'admin_key', ADMIN['X-Admin-Key'])
    return db


def client():
    return TestClient(main.app, base_url='https://testserver')


def login(browser, username='влад', key=KEYS['влад'], headers=WRITE):
    return browser.post(API + '/session', headers=headers,
                        json={'username': username, 'link_key': key})


def shipment(number='Фларп/001', day='2026-11-09', gtin=GTIN):
    return make_xlsx({
        'Документ': [('Поле', 'Значение'), ('Номер документа', number), ('Дата документа', day),
                     ('ИНН организации', ORG), ('Получатель', 'ООО «Дефайнум»'),
                     ('ИНН получателя', '1234567890')],
        'Товары': [('PN', 'GTIN', 'Количество'), ('PN-1', gtin, 100)],
    })


def upload(browser, content, headers=WRITE):
    return browser.post(API + '/tasks', headers=headers, files={'file': ('shipment.xlsx', content)})


def test_sales_session_needs_the_matching_link_and_has_no_admin_access(store, monkeypatch):
    browser = client()
    assert browser.get(API + '/tasks').status_code == 401
    assert browser.get(API + '/template').status_code == 401
    assert login(browser, headers={}).status_code == 403
    assert login(browser, username='полина').status_code == 403
    result = login(browser, username=' Влад ')
    assert result.json() == {'username': 'влад'}
    cookie = result.headers['set-cookie'].lower()
    assert all(attribute in cookie for attribute in ('httponly', 'secure', 'samesite=strict', 'path=' + API))
    assert browser.get(API + '/template').status_code == 200
    assert browser.get('/api/admin/warehouse/documents').status_code == 403
    assert upload(browser, shipment(), headers={}).status_code == 403
    assert browser.delete(API + '/session').status_code == 403
    assert browser.delete(API + '/session', headers=WRITE).status_code == 200
    assert browser.get(API + '/tasks').status_code == 401

    login(browser)
    monkeypatch.setattr(main.settings, 'sales_shipment_links', {'влад': 'C' * 43})
    assert browser.get(API + '/session').status_code == 401
    assert login(browser).status_code == 403
    monkeypatch.setattr(main.settings, 'sales_shipment_links', {})
    assert browser.get('/sales-shipments').status_code == 404
    assert browser.get(API + '/tasks').status_code == 404


def test_sales_task_is_visible_to_warehouse_and_only_the_author_in_sales(store):
    manager, colleague, warehouse = client(), client(), client()
    login(manager)
    login(colleague, username='полина', key=KEYS['полина'])
    assert upload(manager, shipment(gtin='')).status_code == 422
    response = upload(manager, shipment())
    assert response.status_code == 201
    task = response.json()
    assert set(task) == {'number', 'import_warnings'}

    docs = warehouse.get('/api/warehouse/documents').json()['documents']
    assert [(doc['number'], doc['status'], doc['sales_username']) for doc in docs] == [
        (task['number'], 'active', 'влад')]
    document_id = docs[0]['id']
    assert colleague.get(API + '/tasks?username=влад').json() == {'tasks': []}
    assert manager.get(API + '/tasks').json()['tasks'][0]['scan_count'] == 0

    path = '/api/warehouse/documents/' + document_id
    code = '01' + GTIN + '21INTRODUCEDQ100\x1d91EE12\x1d92ABCDcrypto+/='
    assert warehouse.post(path + '/scans', json={'code': code}).status_code == 200
    assert manager.get(API + '/tasks').json()['tasks'][0]['scan_count'] == 1
    assert warehouse.post(path + '/check').json()['can_complete']
    completed = warehouse.post(path + '/complete').json()
    task = manager.get(API + '/tasks').json()['tasks'][0]
    assert task['status'] == 'completed' and task['completed_at']
    assert not {'scans', 'expected', 'result', 'runtime_settings'} & task.keys()
    assert code not in str(task)
    archive = warehouse.post('/api/admin/warehouse/documents/' + document_id + '/archive', headers=ADMIN,
                             json={'version': completed['version'], 'revision': completed['revision'],
                                   'confirmation': completed['number']})
    assert archive.status_code == 200
    assert manager.get(API + '/tasks').json()['tasks'][0]['archived_at']


def test_concurrent_uploads_create_one_task_and_respect_existing_admin_documents(store):
    browsers = [client(), client()]
    for browser, (username, key) in zip(browsers, KEYS.items()):
        login(browser, username, key)
    content = shipment()
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda browser: upload(browser, content), browsers))
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(store.list(admin=True)) == 1
    repeated_by_admin = browsers[0].post('/api/admin/warehouse/documents', headers=ADMIN,
                                         data={'kind': 'shipment'}, files={'file': ('shipment.xlsx', content)})
    assert repeated_by_admin.status_code == 409

    content = shipment(number='Фларп/002', day='20261109')
    admin = browsers[0].post('/api/admin/warehouse/documents', headers=ADMIN, data={'kind': 'shipment'},
                             files={'file': ('shipment.xlsx', content)})
    assert admin.status_code == 200
    assert admin.json()['status'] == 'draft'
    assert upload(browsers[1], shipment(number='Фларп/002')).json()['code'] == 'shipment_exists'


def test_sales_configuration_normalizes_names_and_rejects_a_shared_key(monkeypatch):
    monkeypatch.setenv('SALES_SHIPMENT_LINKS', '{" Влад ":"' + KEYS['влад'] + '"}')
    assert Settings().sales_shipment_links == {'влад': KEYS['влад']}
    monkeypatch.setenv('SALES_SHIPMENT_LINKS', '{"a":"'+ KEYS['влад'] + '","b":"' + KEYS['влад'] + '"}')
    with pytest.raises(ValueError, match='SALES_SHIPMENT_LINKS'):
        Settings()
