"""Core HTTP path stays independent of UPD/customs queries and their health."""
import json
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from config import settings
from service import Service
from tokens import FileTokenStore
from trueapi import INFO_PATH, SHORT_LIST_PATH, TrueApiClient
from warehouse_store import WarehouseStore

GTIN = '04640638345218'
CIS = '01'+GTIN+'21INTRODUCEDQ6000'
CODE = CIS+'\x1d91EE12\x1d92ABCD+/='
ORG = '0000000001'


@pytest.fixture
def core(monkeypatch, tmp_path):
    state = {'calls': [], 'info_timeout': False, 'gtd_error': True}
    def upstream(request):
        state['calls'].append((request.url.path, json.loads(request.content)))
        if request.url.path == INFO_PATH:
            if state['info_timeout']:
                raise httpx.ReadTimeout('slow True API', request=request)
            return httpx.Response(200, json=[{'cisInfo': {'requestedCis':cis,'cis':cis,'gtin':GTIN,
                'productName':'Test PN', 'status':'INTRODUCED', 'ownerInn':ORG,
                'ownerName':'Organization', 'quantityInPack':6000}} for cis in json.loads(request.content)])
        if request.url.path == SHORT_LIST_PATH:
            if state['gtd_error']:
                return httpx.Response(404,json={'error_message':'method unavailable'})
            return httpx.Response(200,json=[{'result':{'requestedCis':CIS,'gtin':GTIN,
                'specialAttributes':{'approvementDocument':{'declarationRegNumber':'10131020/061026/5125218'}}}}])
        raise AssertionError('Unexpected upstream path: '+request.url.path)
    http=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    token_store=FileTokenStore(tmp_path/'tokens.json');token_store.set(ORG,'private-token')
    svc=Service(TrueApiClient('https://trueapi.test',http=http),token_store)
    store=WarehouseStore(tmp_path/'warehouse.db')
    svc.policy_getter=lambda:store.application_settings()['values']
    monkeypatch.setattr(main,'svc',svc)
    monkeypatch.setattr(main,'warehouse',store)
    monkeypatch.setattr(settings,'admin_key','core-admin-key')
    monkeypatch.setattr(settings,'mode','live')
    with TestClient(main.app) as client:
        yield client,svc,state
    # MockTransport has no persistent connection pool; no real network requests.


def test_actual_scan_http_path_and_batch_still_work(core):
    client,svc,state=core
    response=client.post('/api/scan',json={'code':CODE})
    assert response.status_code==200,response.text
    data=response.json()
    assert data['api_checked'] and data['status']=='INTRODUCED'
    assert data['quantity_in_pack']==6000 and data['ours'] is True
    assert 'private-token' not in response.text
    assert state['calls']==[(INFO_PATH,[CIS])]
    assert client.get('/api/status').json()['trueapi']['state']=='ok'
    response=client.post('/api/scan_batch',json={'codes':[CODE,CODE.replace('6000','6001')]})
    assert response.status_code==200,response.text
    assert all(r['api_checked'] and r['error_category'] is None for r in response.json()['results'])
    assert all(path==INFO_PATH for path,_ in state['calls'])


def test_gtd_failure_does_not_declare_scanner_unavailable(core):
    client,svc,state=core
    assert client.post('/api/scan',json={'code':CODE}).status_code==200
    before=svc.trueapi_state()
    response=client.post('/api/admin/warehouse/gtd',json={'code':CODE,'org_inn':ORG},headers={'X-Admin-Key':'core-admin-key'})
    assert response.status_code==502
    assert svc.trueapi_state()==before
    status=client.get('/api/status').json()
    assert status['backend']=='online' and status['trueapi']['state']=='ok'
    assert client.post('/api/scan',json={'code':CODE}).json()['api_checked']


def test_slow_trueapi_is_categorized_result_and_backend_stays_online(core):
    client,svc,state=core
    state['info_timeout']=True
    response=client.post('/api/scan',json={'code':CODE})
    assert response.status_code==200,response.text
    assert response.json()['error_category']=='timeout'
    assert response.json()['api_checked'] is True
    status=client.get('/api/status').json()
    assert status['backend']=='online' and status['trueapi']['last_error']=='timeout'


@pytest.mark.asyncio
@pytest.mark.parametrize('batch',[False,True])
@pytest.mark.parametrize('gtd_error',[False,True])
async def test_optional_gtd_operations_do_not_overwrite_core_health(core,batch,gtd_error):
    _,svc,state=core
    # A successful GTD lookup must not hide a failed core info operation either.
    svc._mark_trueapi_error('timeout')
    before=svc.trueapi_state();state['gtd_error']=gtd_error
    from models import AppError
    async def query():
        if batch:return await svc.lookup_gtd_batch([CIS],ORG)
        return await svc.lookup_gtd(CODE,ORG)
    if gtd_error:
        with pytest.raises(AppError):await query()
    else:
        await query()
    assert svc.trueapi_state()==before


@pytest.mark.skipif(shutil.which('node') is None,reason='Node is required for fetch regression checks')
def test_browser_request_deadlines_and_failure_categories():
    result=subprocess.run(['node',str(Path(__file__).with_name('api_request.test.cjs'))],
                          capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_scanner_page_loads_request_helper_before_app(core):
    client,_,_=core
    page=client.get('/')
    assert page.status_code==200
    assert page.text.index('/static/api-request.js') < page.text.index('/static/app.js')
