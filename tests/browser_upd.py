"""Optional full-browser XML workflow with synthetic data and mocked True API.
Run: python tests/browser_upd.py (requires Playwright Chromium).
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]

if '--server' in sys.argv:
    import main
    from test_upd import doc, GTIN, ORG, BUYER, DT1, DT2
    store=main.warehouse
    d=store.create(dict(kind='shipment',number='T1',date='2026-11-05',org_inn=ORG,
                       org_name='Тест',recipient_name='Покупатель',recipient_inn=BUYER),
                   [dict(gtin=GTIN,pn='SCAN-PN',pn_source='manual',quantity=3)],[])
    data=doc()
    with store.connection(True) as c:
        c.execute("UPDATE warehouse_documents SET status='completed',result=?,result_version=version WHERE id=?",(json.dumps(data['result']),d['id']))
        for s in data['scans']:
            c.execute('INSERT INTO warehouse_scans VALUES (?,?,?,?,?,?,?,?)',
                      (s['id'],d['id'],s['cis'],'raw','full',GTIN,1,'2026-11-05'))
    async def lookup(cises,inn,**kwargs):return dict(zip(cises,[DT1,DT2]))
    main.svc.lookup_gtd_batch=lookup
    Path(os.environ['UPD_TEST_DIR'],'id').write_text(d['id'])
    import uvicorn
    uvicorn.run(main.app,host='127.0.0.1',port=int(os.environ['UPD_TEST_PORT']),log_level='warning')
    sys.exit()

from playwright.sync_api import sync_playwright, expect
from test_upd import xml, parse_xml

with tempfile.TemporaryDirectory(prefix='upd-browser-') as directory:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    env={**os.environ,'GIS_MODE':'mock','ADMIN_KEY':'upd-browser-key',
         'ORGANIZATIONS':'1000000001=Продавец','UPD_TEST_DIR':directory,'UPD_TEST_PORT':str(port),
         'WAREHOUSE_DB':str(Path(directory,'warehouse.db')),'TOKENS_FILE':str(Path(directory,'tokens.json'))}
    server=subprocess.Popen([sys.executable,__file__,'--server'],cwd=ROOT,env=env)
    base=f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:urllib.request.urlopen(base+'/api/status',timeout=1);break
            except OSError:time.sleep(.1)
        document_id=Path(directory,'id').read_text()
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox'], executable_path=shutil.which('chromium'))
            page=browser.new_page(viewport={'width':1280,'height':1000})
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(base+'/admin?kind=shipment&doc='+document_id)
            page.locator('#admin-key').fill('upd-browser-key');page.locator('#login button').click()
            expect(page.locator('#doc-title')).to_contain_text('T1')
            page.locator('#upd-panel summary').click()
            page.locator('#upd-file').set_input_files({'name':'test.xml','mimeType':'application/xml','buffer':xml()})
            page.locator('#upd-upload-form button').click()
            expect(page.locator('#upd-mapping-form')).to_be_visible()
            expect(page.locator('#upd-mapping-rows')).to_contain_text('XML-PN')
            page.locator('[data-upd-row="0"]').select_option('pn:0')
            page.locator('#upd-save-links').check()
            page.locator('#upd-fill-gtd').check()
            page.locator('#upd-mapping-form button').click()
            expect(page.locator('#upd-status')).to_contain_text('Сохранено связей: 1')
            expect(page.locator('#upd-preview tbody tr')).to_have_count(2)
            with page.expect_download() as download:
                page.locator('#upd-download').click()
            parse_xml(Path(download.value.path()).read_bytes())
            # Upload again: saved PN alias must be selected without manual choice.
            page.locator('#upd-file').set_input_files({'name':'next.xml','mimeType':'application/xml','buffer':xml()})
            page.locator('#upd-upload-form button').click()
            expect(page.locator('[data-upd-row="0"]')).to_have_value('pn:0')
            expect(page.locator('#upd-mapping-rows')).to_contain_text('Сохранённая связь')
            expect(page.locator('#upd-download')).to_be_hidden()
            assert not errors,errors
            browser.close()
        print('UPD browser workflow passed: upload, PN selection, saved alias, split preview, XML download')
    finally:
        server.terminate();server.wait(timeout=5)
