import io
from threading import Event

import openpyxl
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from config import settings
from km_report import read_codes, process_report
from service import Service
from tokens import FileTokenStore
from warehouse_excel import make_xlsx, read_excel_strings
from warehouse_routes import install_warehouse
from warehouse_store import WarehouseStore, WarehouseError

GTIN = '04640638345218'
CIS = '01' + GTIN + '21ABC123'
OTHER = '01' + GTIN + '21SECOND'
DT = '10131020/061026/5125218'
ORG = '0000000001'


class Api:
    def __init__(self):
        self.calls = []
        self.quantity = 6000
        self.omit = False

    async def info(self, cises, token):
        self.calls.append(('info', cises))
        return [{'cisInfo': dict(requestedCis=cis, gtin=GTIN, status='INTRODUCED',
                                quantityInPack=self.quantity)} for cis in reversed(cises)]

    async def short_info(self, cises, token):
        self.calls.append(('short', cises))
        return [{'result': dict(requestedCis=cis, gtin=GTIN, specialAttributes={
            'approvementDocument': {'declarationRegNumber': DT if cis == CIS else DT + '9'}})}
            for cis in cises if not self.omit]


@pytest.fixture
def service(tmp_path):
    tokens = FileTokenStore(tmp_path / 'tokens.json')
    tokens.set(ORG, 'secret')
    return Service(Api(), tokens)


def workbook():
    return make_xlsx({'Коды': [['DataMatrix'], [CIS + '<GS>91EE1234<GS>92abcdef<GS>3012'],
                              [OTHER], [CIS], ['invalid']],
                      'Ещё': [['КМ'], [CIS + '\x1d91EE1234\x1d92abcdef']]})


def test_reader_preserves_gs_and_rows():
    rows = read_codes(workbook())
    assert len(rows) == 5
    assert rows[0]['cis'] == CIS
    assert rows[3]['cis'] == '' and rows[3]['reason']
    assert '\x1d' in rows[-1]['raw']
    with pytest.raises(WarehouseError):
        read_codes(make_xlsx({'Data': [['Wrong'], [CIS]]}))


async def test_report_uses_api_quantity_not_ai30_and_deduplicates(service, tmp_path):
    store = WarehouseStore(tmp_path / 'db')
    job_id = store.gtd_excel_create('codes.xlsx', workbook())
    await process_report(store, service, job_id, ORG)
    job = store.gtd_excel_download(job_id)
    assert job['report'] == dict(kind='km-report', total=5, unique=2, with_quantity=4, with_gtd=4)
    book = openpyxl.load_workbook(io.BytesIO(job['output']))
    sheet = book.active
    assert sheet['F2'].value == 6000  # AI 30 in the source says 12.
    assert sheet['G2'].value == DT
    assert sheet['G3'].value == DT + '9'  # API replies were reordered.
    assert sheet['F5'].value is None and sheet['I5'].value
    assert read_excel_strings(job['output'])['КМ и ГТД']['C6'] == read_codes(workbook())[-1]['raw']
    assert service.client.calls == [('info', [CIS, OTHER]), ('short', [CIS, OTHER])]


@pytest.mark.parametrize('qty', [None, 0, -1, True, 'bad'])
async def test_missing_or_invalid_quantity_is_blank_but_gtd_survives(service, qty):
    service.client.quantity = qty
    result = (await service.lookup_km_report_batch([CIS], ORG))[CIS]
    assert result['quantity_in_pack'] is None
    assert result['declaration_number'] == DT
    assert 'quantityInPack' in result['message']


async def test_missing_gtd_does_not_erase_quantity(service):
    service.client.omit = True
    result = (await service.lookup_km_report_batch([CIS], ORG))[CIS]
    assert result['quantity_in_pack'] == 6000
    assert result['declaration_number'] is None
    assert 'не вернул' in result['message']


async def test_conflicting_identities_do_not_assign_quantity(service):
    async def conflict(cises, token):
        return [{'requestedCis': CIS, 'cisInfo': dict(cis=OTHER, gtin=GTIN, quantityInPack=99)}]
    service.client.info = conflict
    result = await service.lookup_km_report_batch([CIS, OTHER], ORG)
    assert all(row['quantity_in_pack'] is None for row in result.values())
    assert result[CIS]['declaration_number'] == DT


def test_report_routes_protected_and_downloadable(monkeypatch, service, tmp_path):
    store = WarehouseStore(tmp_path / 'db')
    finished = Event()
    finish = store.gtd_excel_finish
    def notify(*args):
        finish(*args)
        finished.set()
    monkeypatch.setattr(store, 'gtd_excel_finish', notify)
    monkeypatch.setattr(settings, 'admin_key', 'report-key')
    app = FastAPI()
    install_warehouse(app, lambda: service, lambda: store, Jinja2Templates(directory='templates'))
    base = '/api/admin/warehouse/km-report'
    headers = {'X-Admin-Key': 'report-key'}
    with TestClient(app) as client:
        assert client.get(base + '/template').status_code == 403
        assert client.post(base, files={'file': ('c.xlsx', workbook())}, data={'org_inn': ORG}).status_code == 403
        result = client.post(base, files={'file': ('c.xlsx', workbook())}, data={'org_inn': ORG}, headers=headers)
        assert result.status_code == 200, result.text
        job_id = result.json()['id']
        assert finished.wait(5)
        status = client.get(base + '/' + job_id, headers=headers)
        assert status.json()['status'] == 'completed'
        assert 'source' not in status.json()
        assert client.get(base + '/' + job_id + '/download').status_code == 403
        output = client.get(base + '/' + job_id + '/download', headers=headers)
        assert output.status_code == 200
        assert openpyxl.load_workbook(io.BytesIO(output.content)).active['F2'].value == 6000
