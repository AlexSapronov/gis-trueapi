"""UPD enrichment: identity, indivisible packs, exact money, durable aliases, auth."""
from copy import deepcopy
from decimal import Decimal

import pytest
from lxml import etree as E
from fastapi.testclient import TestClient

import main
from config import settings
from models import AppError
from service import Service
from mockclient import MockTrueApiClient
from tokens import FileTokenStore
from upd_xml import inspect_xml, parse_xml, allocate, enrich_xml, money_parts
from warehouse_store import WarehouseStore, WarehouseError

GTIN = '04640638345218'
ORG = '1000000001'
BUYER = '1234567890'
DT1, DT2 = '10131020/061026/5125218', '10131020/071026/5125219'


def xml(quantity=3):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Файл ИдФайл="ON_NSCHFDOPPR_receiver_sender_20261105_00000000-0000-0000-0000-000000000001_0_0_0_0_0_00" ВерсФорм="5.03" ВерсПрог="Test">
<Документ КНД="1115131" Функция="СЧФДОП" НаимДокОпр="УПД" ПоФактХЖ="Отгрузка" ДатаИнфПр="05.11.2026" ВремИнфПр="12.00.00" НаимЭконСубСост="Тест">
<СвСчФакт НомерДок="T1" ДатаДок="05.11.2026">
<СвПрод><ИдСв><СвЮЛУч НаимОрг="Продавец" ИННЮЛ="{ORG}" КПП="100001001"/></ИдСв><Адрес><АдрИнф КодСтр="643" НаимСтран="Россия" АдрТекст="Тест"/></Адрес></СвПрод>
<СвПокуп><ИдСв><СвЮЛУч НаимОрг="Покупатель" ИННЮЛ="{BUYER}" КПП="100001001"/></ИдСв><Адрес><АдрИнф КодСтр="643" НаимСтран="Россия" АдрТекст="Тест"/></Адрес></СвПокуп>
<ДенИзм КодОКВ="643" НаимОКВ="Российский рубль"/>
</СвСчФакт><ТаблСчФакт>
<СведТов НомСтр="1" НаимТов="XML-PN (Светодиод)" ОКЕИ_Тов="796" НаимЕдИзм="шт" КолТов="{quantity}" ЦенаТов="3.33" СтТовБезНДС="10.01" НалСт="22%" СтТовУчНал="12.21">
<СвДТ КодПроисх="156" НомерДТ="{DT1}"/><ДопСведТов ПрТовРаб="1" КодТов="БП-001"><КрНаимСтрПр>КИТАЙ</КрНаимСтрПр></ДопСведТов>
<Акциз><БезАкциз>без акциза</БезАкциз></Акциз><СумНал><СумНал>2.20</СумНал></СумНал></СведТов>
<ВсегоОпл СтТовБезНДСВсего="10.01" СтТовУчНалВсего="12.21" КолНеттоВс="{quantity}"><СумНалВсего><СумНал>2.20</СумНал></СумНалВсего></ВсегоОпл>
</ТаблСчФакт><СвПродПер><СвПер СодОпер="Товары переданы" ДатаПер="05.11.2026"><ОснПер РеквНаимДок="Договор" РеквНомерДок="1" РеквДатаДок="01.11.2026"/></СвПер></СвПродПер>
<Подписант Должн="Бухгалтер" ТипПодпис="1" СпосПодтПолном="6"><ФИО Фамилия="Тест" Имя="Тест"/></Подписант>
</Документ></Файл>'''.encode()


def doc(quantities=(1, 2)):
    scans = [dict(id=str(i), gtin=GTIN, cis=GTIN+'S'+str(i), raw_code='unused') for i in range(len(quantities))]
    return dict(id='doc', kind='shipment', status='completed', org_inn=ORG, recipient_inn=BUYER,
                version=5, revision=6, check_current=True, deleted_at=None,
                lines=[dict(gtin=GTIN,pn='SCAN-PN',quantity=sum(quantities))], scans=scans,
                result=dict(ok=True,codes=[dict(scan_id=s['id'], quantity=q, errors=[]) for s,q in zip(scans,quantities)]))


def prepare(content=None, document=None, mapping=None):
    content, document = content or xml(), document or doc()
    mapping = mapping or {'0':'SCAN-PN'}
    allocated=allocate(parse_xml(content),document,mapping)
    return enrich_xml(content,document,mapping,allocated,{s['cis']:dt for s,dt in zip([s for group in allocated for s in group],[DT1,DT2,DT2])})


def test_inspection_pn_not_internal_code_and_saved_alias():
    result=inspect_xml(xml(),doc(),{'XML-PN':'SCAN-PN'})
    assert result['rows'][0]['pn']=='XML-PN'
    assert result['rows'][0]['target_pn']=='SCAN-PN'
    assert result['rows'][0]['source']=='saved'
    assert result['choices'][0]['quantity']==3


def test_split_preserves_totals_currency_price_country_and_ki():
    output,filename,summary=prepare()
    root=parse_xml(output)
    rows=root.findall('Документ/ТаблСчФакт/СведТов')
    assert [r.get('КолТов') for r in rows]==['1','2']
    assert [r.get('НомСтр') for r in rows]==['1','2']
    for attr,total in [('СтТовБезНДС','10.01'),('СтТовУчНал','12.21')]:
        assert sum(Decimal(r.get(attr)) for r in rows)==Decimal(total)
    assert sum(Decimal(r.findtext('СумНал/СумНал')) for r in rows)==Decimal('2.20')
    assert all(Decimal(r.get('СтТовБезНДС'))+Decimal(r.findtext('СумНал/СумНал'))==Decimal(r.get('СтТовУчНал')) for r in rows)
    assert [r.find('СвДТ').get('НомерДТ') for r in rows]==[DT1,DT2]
    assert all(r.find('СвДТ').get('КодПроисх')=='156' and r.get('ЦенаТов')=='3.33' for r in rows)
    assert root.findall('.//КИЗ')[0].text=='01'+GTIN+'21S0'
    assert E.tostring(root.find('.//ВсегоОпл'))==E.tostring(parse_xml(xml()).find('.//ВсегоОпл'))
    assert filename==root.get('ИдФайл')+'.xml'
    assert root.get('ИдФайл').split('_')[-5]=='1'
    assert root.get('ИдФайл')!=parse_xml(xml()).get('ИдФайл')
    assert [s['quantity'] for s in summary]==[1,2]


def test_same_dt_one_row_and_crypto_not_exported():
    d=doc();a=allocate(parse_xml(xml()),d,{'0':'SCAN-PN'})
    out,_,summary=enrich_xml(xml(),d,{'0':'SCAN-PN'},a,{s['cis']:DT1 for group in a for s in group})
    r=parse_xml(out).find('.//СведТов')
    assert r.get('КолТов')=='3' and r.get('СтТовБезНДС')=='10.01'
    assert len(r.findall('.//КИЗ'))==2 and len(summary)==1


@pytest.mark.parametrize('mapping', [{}, {'0':None}, {'0':'unknown'}, {'0':'SCAN-PN','1':None}])
def test_bad_or_incomplete_mapping(mapping):
    with pytest.raises(WarehouseError):allocate(parse_xml(xml()),doc(),mapping)


def test_no_partial_pack_or_unused_codes():
    with pytest.raises(WarehouseError,match='количество XML'):
        allocate(parse_xml(xml(2)),doc(),{'0':'SCAN-PN'})
    with pytest.raises(WarehouseError,match='Не все КМ'):
        allocate(parse_xml(xml(1)),doc(),{'0':'SCAN-PN'})


def test_missing_dt_blocks_export():
    d=doc();a=allocate(parse_xml(xml()),d,{'0':'SCAN-PN'})
    with pytest.raises(WarehouseError,match='номер ГТД'):
        enrich_xml(xml(),d,{'0':'SCAN-PN'},a,{d['scans'][0]['cis']:DT1})


@pytest.mark.parametrize('patch', [dict(org_inn='9999999999'),dict(recipient_inn='9999999999'),dict(check_current=False),dict(status='active')])
def test_wrong_parties_or_stale_document(patch):
    with pytest.raises(WarehouseError):inspect_xml(xml(),{**doc(),**patch},{})


def test_xml_security_and_schema():
    for content in [b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>',
                    b'<x/>',xml().replace(b'5.03',b'5.01'),b'bad',b'x'*(10*1024*1024+1)]:
        with pytest.raises(WarehouseError):parse_xml(content)


def test_many_groups_no_negative_rounding_residual():
    parts=money_parts('.05',[1]*20)
    assert sum(parts)==Decimal('.05') and min(parts)>=0


def test_repeat_pn_rows_consume_each_scan_once():
    root=parse_xml(xml());table=root.find('.//ТаблСчФакт');row=table.find('СведТов');row.set('КолТов','1')
    second=deepcopy(row);second.set('КолТов','2');second.set('НомСтр','2');table.insert(1,second)
    allocated=allocate(root,doc(),{'0':'SCAN-PN','1':'SCAN-PN'})
    assert [[s['id'] for s in a] for a in allocated]==[['0'],['1']]


@pytest.mark.asyncio
async def test_batch_gtd_pairs_by_identity_not_response_order(tmp_path):
    tokens=FileTokenStore(tmp_path/'tokens');tokens.set('0000000001','token')
    client=MockTrueApiClient();service=Service(client,tokens);scans=[{**s, 'cis':'01'+GTIN+'21'+s['cis'][14:]} for s in doc()['scans']]
    async def short_info(cises,token):
        return [{'result':{'requestedCis':s['cis'],'gtin':GTIN,'specialAttributes':{'approvementDocument':{'declarationRegNumber':dt}}}}
                for s,dt in reversed(list(zip(scans,[DT1,DT2])))]
    client.short_info=short_info
    assert await service.lookup_gtd_batch([s['cis'] for s in scans],'0000000001')=={scans[0]['cis']:DT1,scans[1]['cis']:DT2}
    async def missing(cises,token):return []
    client.short_info=missing
    with pytest.raises(AppError):await service.lookup_gtd_batch([s['cis'] for s in scans],'0000000001')


def test_routes_protected_roundtrip_restart_aliases_and_stale(monkeypatch,tmp_path):
    store=WarehouseStore(tmp_path/'warehouse.sqlite3')
    header=dict(kind='shipment',number='T1',date='2026-11-05',org_inn=ORG,org_name='Тест',recipient_name='Тест',recipient_inn=BUYER)
    document=store.create(header,[dict(gtin=GTIN,pn='SCAN-PN',pn_source='manual',quantity=3)],[])
    d=doc();d['id']=document['id']
    # Seed a completed successfully checked warehouse snapshot.
    import json
    with store.connection(True) as c:
        c.execute("UPDATE warehouse_documents SET status='completed',result=?,result_version=version WHERE id=?",(json.dumps(d['result']),d['id']))
        for s in d['scans']:
            c.execute('INSERT INTO warehouse_scans VALUES (?,?,?,?,?,?,?,?)',(s['id'],d['id'],s['cis'],'raw','full',GTIN,1,'2026-11-05'))
    class Api:
        async def lookup_gtd_batch(self,cises,inn):return dict(zip(cises,[DT1,DT2]))
    monkeypatch.setattr(settings,'admin_key','upd-test-key')
    from warehouse_routes import install_warehouse
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    app=FastAPI();install_warehouse(app,lambda:Api(),lambda:store,Jinja2Templates(directory='templates'))
    with TestClient(app) as client:
        base='/api/admin/warehouse/documents/'+d['id']+'/upd'
        assert client.post(base,files={'file':('test.xml',xml())}).status_code==403
        headers={'X-Admin-Key':'upd-test-key'}
        # Authenticate through the supported API-key header.
        response=client.post(base,files={'file':('test.xml',xml())},headers=headers)
        assert response.status_code==200,response.text
        upload=response.json()['upload_id'];url=base+'/'+upload
        assert client.get(url+'/download',headers=headers).status_code==409
        def unexpected_api():
            raise AssertionError('GTD lookup must not be used with unchecked checkbox')
        # Default mode must work even if the GTD API is unavailable.
        from unittest.mock import patch
        with patch.object(Api, 'lookup_gtd_batch', side_effect=unexpected_api):
            plain=client.post(url+'/prepare',json={'mapping':{'0':'SCAN-PN'}},headers=headers)
            assert plain.status_code==200,plain.text
            assert plain.json()['gtd_filled'] is False
            plain_output=client.get(url+'/download',headers=headers)
            assert len(parse_xml(plain_output.content).findall('.//СведТов'))==1
            assert parse_xml(plain_output.content).find('.//СвДТ').get('НомерДТ')==DT1
        response=client.post(url+'/prepare',json={'mapping':{'0':'SCAN-PN'},'save_links':True,'fill_gtd':True},headers=headers)
        assert response.status_code==200,response.text
        assert len(response.json()['rows'])==2
        out=client.get(url+'/download',headers=headers)
        assert out.status_code==200
        parse_xml(out.content)
        restarted=WarehouseStore(store.path)
        assert restarted.upd_aliases(ORG)=={'XML-PN':'SCAN-PN'}
        assert restarted.upd_aliases('other')=={}
        store.edit(d['id'],0,pns={GTIN:'CHANGED'},revision=0)
        assert client.get(url+'/download',headers=headers).status_code==409


def test_unmarked_row_preserved_and_renumbered_after_split():
    r=parse_xml(xml());table=r.find('.//ТаблСчФакт');other=deepcopy(table.find('СведТов'))
    other.set('НомСтр','2');other.set('НаимТов','Немаркируемый товар');table.insert(1,other)
    content=E.tostring(r,encoding="utf-8")
    output,_,_=prepare(content=content,mapping={'0':'SCAN-PN','1':None})
    rows=parse_xml(output).findall('.//СведТов')
    assert len(rows)==3 and rows[-1].get('НомСтр')=='3'
    other.set('НомСтр','3')
    assert E.tostring(rows[-1])==E.tostring(other)


def test_saved_output_not_written_if_warehouse_changed(tmp_path):
    store=WarehouseStore(tmp_path/'state.db')
    d=store.create(dict(kind='shipment',number='T1',date='2026-11-05',org_inn=ORG,
                        org_name='Тест',recipient_name='',recipient_inn=BUYER),
                   [dict(gtin=GTIN,pn='SCAN-PN',pn_source='manual',quantity=3)],[])
    upload=store.upd_create(d,xml())
    store.edit(d['id'],d['version'],pns={GTIN:'NEW'},revision=d['revision'])
    with pytest.raises(WarehouseError,match='изменилась'):
        store.upd_finish(d['id'],upload,b'xml','file.xml',[],{'XML-PN':'SCAN-PN'})
    assert store.upd_aliases(ORG)=={}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['duplicate','mismatch','missing_number','unidentified'])
async def test_batch_rejects_ambiguous_or_missing_declarations(tmp_path,mode):
    tokens=FileTokenStore(tmp_path/'tokens');tokens.set('0000000001','token')
    client=MockTrueApiClient();svc=Service(client,tokens)
    cis='01'+GTIN+'21S1'
    record={'result':{'requestedCis':cis,'cis':cis,'gtin':GTIN,'specialAttributes':{
        'approvementDocument':{'declarationRegNumber':DT1}}}}
    if mode=='mismatch':record['result']['cis']='01'+GTIN+'21S2'
    if mode=='missing_number':record['result']['specialAttributes']={}
    if mode=='unidentified':record['result'].pop('requestedCis');record['result'].pop('cis')
    async def response(cises,token):return [record,deepcopy(record)] if mode=='duplicate' else [record]
    client.short_info=response
    with pytest.raises(AppError):await svc.lookup_gtd_batch([cis],'0000000001')


@pytest.mark.parametrize('existing', ['none', 'multiple'])
def test_optional_gtd_keeps_original_declarations(existing):
    root=parse_xml(xml())
    row=root.find('.//СведТов')
    if existing=='none':
        row.remove(row.find('СвДТ'))
    else:
        declaration=deepcopy(row.find('СвДТ'))
        declaration.set('НомерДТ',DT2)
        declaration.set('КодПроисх','840')
        row.insert(1,declaration)
    original=[E.tostring(n,encoding='utf-8') for n in row.findall('СвДТ')]
    content=E.tostring(root,encoding='utf-8')
    allocated=allocate(root,doc(),{'0':'SCAN-PN'})
    output,_,_=enrich_xml(content,doc(),{'0':'SCAN-PN'},allocated,None)
    result=parse_xml(output)
    assert len(result.findall('.//СведТов'))==1
    assert len(result.findall('.//КИЗ'))==2
    assert [E.tostring(n,encoding='utf-8') for n in result.findall('.//СвДТ')]==original
