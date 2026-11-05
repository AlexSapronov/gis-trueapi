"""Bulk ERP customs correction uses actual shipment KIs and preserves workbook data."""
import asyncio
from datetime import datetime
from decimal import Decimal
import io
import json

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
import openpyxl
import pytest

from config import settings
from gtd_excel import load_book, read_catalog, read_rows, process_excel_job
from warehouse_excel import make_xlsx
from warehouse_routes import install_warehouse
from warehouse_store import WarehouseStore, WarehouseError

ORG='1000000001';BUYER='1234567890';GTIN='04640638345218'
DT1='10131020/061026/5125218';DT2='10131020/071026/5125219'
HEAD=['Number','FromINN','ToINN','IsReturn','Date','GoodId','GoodName','Amount','Price','Declaration','GoodsItemTotal','UnitCode']


def row(qty=6000,dt=DT1,pn='PN-1 (Светодиод)',good_id=123,total=60):
    return ['T1',int(ORG),int(BUYER),0,datetime(2026,11,5),good_id,pn,qty,.01,dt,total,796]


def workbook(rows=None):return make_xlsx({'Лист1':[HEAD]+(rows or [row()])})


@pytest.fixture
def store(tmp_path):return WarehouseStore(tmp_path/'warehouse.db')


def completed(store,quantities=(6000,),pn='PN-1'):
    d=store.create(dict(kind='shipment',number='T1',date='2026-11-05',org_inn=ORG,
                        org_name='Тест',recipient_name='Покупатель',recipient_inn=BUYER),
                   [dict(gtin=GTIN,pn=pn,pn_source='manual',quantity=sum(quantities))],[])
    codes=[]
    with store.connection(True) as c:
        for i,qty in enumerate(quantities):
            scan_id='s'+str(i)+d['id'];cis=GTIN+'X'+str(i)
            c.execute('INSERT INTO warehouse_scans VALUES (?,?,?,?,?,?,?,?)',
                      (scan_id,d['id'],cis,'raw','full',GTIN,1,'2026-11-05'))
            codes.append(dict(scan_id=scan_id,quantity=qty,errors=[]))
        c.execute("UPDATE warehouse_documents SET status='completed',result=?,result_version=version WHERE id=?",
                  (json.dumps({'ok':True,'codes':codes}),d['id']))
    return store.get(d['id'])


class Api:
    def __init__(self,dts=None):self.dts=dts;self.calls=[]
    async def lookup_gtd_batch(self,cises,inn,**kwargs):
        self.calls.append((cises,inn,kwargs))
        return {cis:self.dts[i] if self.dts is not None else DT2 for i,cis in enumerate(cises)}


def run(store,content=None,api=None):
    job=store.gtd_excel_create('source.xlsx',content or workbook())
    asyncio.run(process_excel_job(store,api or Api(),job))
    result=store.gtd_excel_get(job,files=True)
    assert result['status']=='completed',result['message']
    return result,load_book(result['output'])


def test_catalog_accepts_numeric_ids_and_13_digit_gtin(store):
    content=make_xlsx({'Catalog':[['GoodID','GTIN'],[123,GTIN[1:]],['000123',GTIN]]})
    mapping=read_catalog(content)
    assert mapping=={'123':GTIN,'000123':GTIN}
    assert store.gtd_catalog_save(mapping)['added']==2
    assert store.gtd_catalog_save(mapping)['unchanged']==2
    assert WarehouseStore(store.path).gtd_catalog()['rows']==store.gtd_catalog()['rows']


@pytest.mark.parametrize('rows',[
    [['GoodID','GTIN'],[123,'bad']],
    [['GoodID','GTIN'],[123,GTIN],[123,'07712345678907']],
    [['GoodID','GTIN'],[123,None]],
])
def test_catalog_rejects_invalid_or_conflicting_mappings(rows):
    with pytest.raises(WarehouseError):read_catalog(make_xlsx({'Catalog':rows}))


def test_goodid_match_overrides_different_pn_names(store):
    completed(store,pn='Warehouse-PN')
    store.gtd_catalog_save({'123':GTIN})
    api=Api();result,book=run(store,workbook([row(pn='ERP name differs')]),api)
    assert result['report']['changed']==1
    assert result['report']['rows'][0]['match_source']=='GoodID → GTIN'
    assert api.calls[0][0]==['01'+GTIN+'21X0']
    assert book['Лист1']['J2'].value==DT2


def test_replaces_only_declaration_and_highlights_changed_row(store):
    completed(store)
    content=workbook()
    before=load_book(content)
    result,book=run(store,content)
    sheet=book['Лист1']
    for col in range(1,len(HEAD)+1):
        if col!=10:assert sheet.cell(2,col).value==before['Лист1'].cell(2,col).value
        assert sheet.cell(2,col).fill.fgColor.rgb=='00FFF2CC'
    assert sheet['J2'].value==DT2
    assert sheet['J2'].data_type=='s'
    assert result['report']['changed']==1 and result['report']['unconfirmed']==0
    assert 'Проверка ГТД' in book.sheetnames
    assert book['Проверка ГТД']['E2'].value==DT1


def test_same_declaration_no_highlight_and_suffix_not_invented(store):
    completed(store)
    result,book=run(store,api=Api([DT1]))
    assert result['report']['confirmed']==1 and result['report']['changed']==0
    assert book['Лист1']['J2'].fill.patternType is None
    result,book=run(store,workbook([row(dt=DT1+'/19')]),Api([DT1]))
    assert result['report']['changed']==1
    assert book['Лист1']['J2'].value==DT1


@pytest.mark.parametrize('case',['no_dt','no_doc','wrong_recipient','wrong_date','wrong_quantity','wrong_pn'])
def test_unconfirmed_preserves_original_values(store,case):
    if case!='no_doc':completed(store)
    data=row()
    if case=='wrong_recipient':data[2]=9999999999
    if case=='wrong_date':data[4]=datetime(2026,11,4)
    if case=='wrong_quantity':data[7]=5999
    if case=='wrong_pn':data[6]='UNKNOWN'
    api=Api([None]) if case=='no_dt' else Api()
    content=workbook([data]);before=load_book(content)
    result,book=run(store,content,api)
    assert result['report']['unconfirmed']==1
    assert result['report']['changed']==0
    assert [c.value for c in book['Лист1'][2]]==[c.value for c in before['Лист1'][2]]
    if case!='no_dt':assert api.calls==[]


def test_multiple_confirmed_declarations_split_a_row_and_preserve_totals(store):
    completed(store,(3000,3000))
    result,book=run(store,api=Api([DT1,DT2]))
    sheet=book['Лист1']
    assert sheet.max_row==3
    assert [sheet.cell(n,8).value for n in (2,3)]==[3000,3000]
    assert [sheet.cell(n,10).value for n in (2,3)]==[DT1,DT2]
    assert sum(Decimal(str(sheet.cell(n,11).value)) for n in (2,3))==60
    assert result['report']['changed']==1


def test_1c_boundaries_rebuilt_over_whole_codes(store):
    completed(store,(6000,)*12)
    source=workbook([row(70404,DT1,total=704.04),row(1596,DT2,total=15.96)])
    result,book=run(store,source,Api([DT1]*11+[DT2]))
    sheet=book['Лист1']
    assert [sheet.cell(n,8).value for n in (2,3)]==[66000,6000]
    assert [sheet.cell(n,10).value for n in (2,3)]==[DT1,DT2]
    assert sum(Decimal(str(sheet.cell(n,11).value)) for n in (2,3))==720
    assert result['report']['changed']==2


def test_unrelated_formulas_preserved_for_simple_replacement(store):
    completed(store)
    source=workbook();book=load_book(source)
    book['Лист1']['M2']='=SUM(H2:H2)'
    stream=io.BytesIO();book.save(stream)
    result,book=run(store,stream.getvalue())
    assert book['Лист1']['M2'].value=='=SUM(H2:H2)' and book['Лист1']['M2'].data_type=='f'
    assert result['report']['changed']==1


def test_structural_change_with_formulas_requires_review(store):
    completed(store,(3000,3000));book=load_book(workbook());book['Лист1']['M2']='=H2'
    stream=io.BytesIO();book.save(stream)
    result,book=run(store,stream.getvalue(),Api([DT1,DT2]))
    assert result['report']['unconfirmed']==1 and book['Лист1'].max_row==2
    assert book['Лист1']['J2'].value==DT1


def test_download_rejects_changed_shipment_or_catalog(store):
    d=completed(store);result,_=run(store)
    store.gtd_excel_download(result['id'])
    store.gtd_catalog_save({'123':GTIN})
    with pytest.raises(WarehouseError,match='Справочник'):store.gtd_excel_download(result['id'])
    result,_=run(store)
    store.edit(d['id'],d['version'],pns={GTIN:'New'},revision=d['revision'])
    with pytest.raises(WarehouseError,match='отгрузка изменилась'):store.gtd_excel_download(result['id'])


def test_ambiguous_shipment_not_chosen_arbitrarily(store):
    completed(store);completed(store)
    api=Api();result,_=run(store,api=api)
    assert result['report']['unconfirmed']==1 and api.calls==[]


def test_catalog_and_excel_endpoints_require_admin(store,monkeypatch):
    completed(store)
    from threading import Event
    finished=Event();finish=store.gtd_excel_finish
    def notify(*args):
        finish(*args);finished.set()
    monkeypatch.setattr(store,'gtd_excel_finish',notify)
    monkeypatch.setattr(settings,'admin_key','excel-test-key')
    app=FastAPI();install_warehouse(app,lambda:Api(),lambda:store,Jinja2Templates(directory='templates'))
    with TestClient(app) as client:
        base='/api/admin/warehouse'
        assert client.post(base+'/gtd-excel',files={'file':('test.xlsx',workbook())}).status_code==403
        assert client.get(base+'/gtd-catalog').status_code==403
        headers={'X-Admin-Key':'excel-test-key'}
        catalog=make_xlsx({'Catalog':[['GoodID','GTIN'],[123,GTIN]]})
        result=client.post(base+'/gtd-catalog',files={'file':('catalog.xlsx',catalog)},headers=headers)
        assert result.status_code==200 and result.json()['added']==1
        assert client.get(base+'/gtd-catalog/template',headers=headers).status_code==200
        result=client.post(base+'/gtd-excel',files={'file':('source.xlsx',workbook())},headers=headers)
        assert result.status_code==200,result.text
        job_id=result.json()['id']
        assert finished.wait(5),'Background job did not complete'
        status=client.get(base+'/gtd-excel/'+job_id,headers=headers)
        assert status.status_code==200
        assert 'source' not in status.json() and 'output' not in status.json()
        assert status.json()['status']=='completed'
        output=client.get(base+'/gtd-excel/'+job_id+'/download',headers=headers)
        assert output.status_code==200
        assert load_book(output.content)['Лист1']['J2'].value==DT2
        assert client.get(base+'/gtd-catalog',headers=headers).json()['rows'][0]['gtin']==GTIN


def test_restart_recovers_interrupted_job(store):
    job=store.gtd_excel_create('test.xlsx',workbook())
    store.gtd_excel_recover()
    assert store.gtd_excel_get(job)['status']=='failed'


def test_split_preserves_high_precision_excel_total(store):
    completed(store,(3000,3000))
    result,book=run(store,workbook([row(total=.01001)]),Api([DT1,DT2]))
    assert sum(Decimal(str(book['Лист1'].cell(n,11).value)) for n in (2,3))==Decimal('.01001')


def test_uploaded_reference_is_recognized_and_preserved_when_no_scans_match(tmp_path):
    # This is a structural template test, without committing business data.
    content=make_xlsx({'Лист1':[[None]+HEAD,[None]+row()]})
    store=WarehouseStore(tmp_path/'empty.db')
    result,book=run(store,content)
    source=load_book(content)['Лист1']
    assert [[c.value for c in row] for row in source]==[[c.value for c in row] for row in book['Лист1']]
    assert read_rows(book)[0]['before']==DT1


def test_structural_rows_do_not_duplicate_extra_tax_amounts(store):
    completed(store,(3000,3000))
    content=make_xlsx({'Лист1':[HEAD+['CustomTax'],row()+[100]]})
    result,book=run(store,content,Api([DT1,DT2]))
    assert result['report']['unconfirmed']==1
    assert book['Лист1'].max_row==2 and book['Лист1']['M2'].value==100
