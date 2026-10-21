"""Optional browser smoke test, uses temporary SQLite and mock tokens.
Run: pip install playwright==1.55.0; python -m playwright install chromium
     python tests/browser_warehouse.py
"""
import sys, re, os, socket, subprocess, tempfile, time, urllib.request, shutil
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warehouse_excel import make_xlsx
from playwright.sync_api import sync_playwright, expect

if __name__ == '__main__' and '--scenario' not in sys.argv:
    with tempfile.TemporaryDirectory(prefix='warehouse-browser-') as temp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**os.environ, 'GIS_MODE':'mock', 'ADMIN_KEY':'browser-test-admin',
               'ORGANIZATIONS':'0000000001=Тестовая организация;0000000002=Вторая организация', 'RECEIPT_DEFAULT_ORG':'',
               'WAREHOUSE_DB':str(Path(temp) / 'state.sqlite3'),
               'TOKENS_FILE':str(Path(temp) / 'tokens.json'), 'BUILD_ID':'browser-test-build',
               'WAREHOUSE_BROWSER_BASE':f'http://127.0.0.1:{port}', 'WAREHOUSE_BROWSER_DIR':temp}
        server_code = ("from config import settings; from tokens import FileTokenStore; "
                       "FileTokenStore(settings.tokens_file).set('0000000001','mock-token'); "
                       f"import uvicorn; uvicorn.run('main:app',host='127.0.0.1',port={port},log_level='warning')")
        server = subprocess.Popen([sys.executable, '-c', server_code], cwd=ROOT, env=env)
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(env['WAREHOUSE_BROWSER_BASE'] + '/api/status', timeout=1)
                    break
                except OSError:
                    time.sleep(.1)
            result = subprocess.run([sys.executable, __file__, '--scenario'], cwd=ROOT, env=env)
            if os.environ.get('WAREHOUSE_SCREENSHOTS'):
                target=Path(os.environ['WAREHOUSE_SCREENSHOTS']);target.mkdir(parents=True,exist_ok=True)
                for screenshot in Path(temp).glob('*.png'): shutil.copy2(screenshot,target / screenshot.name)
        finally:
            server.terminate()
            server.wait(timeout=5)
        sys.exit(result.returncode)

BASE = os.environ['WAREHOUSE_BROWSER_BASE']
TEST_DIR = Path(os.environ['WAREHOUSE_BROWSER_DIR'])
GTIN='04640638345218'
def code(q,suffix=''):
 return '01'+GTIN+'21INTRODUCEDQ'+str(q)+suffix+'\x1d91EE12\x1d92ABCD+/='
header=[['Поле','Значение'],['Номер документа','Браузер/001'],['Дата документа','2026-10-21'],['ИНН организации','0000000001'],['Получатель','Тестовый клиент'],['ИНН получателя','1234567890']]
ship=str(TEST_DIR / 'shipment.xlsx')
open(ship,'wb').write(make_xlsx({'Документ':header,'Товары':[['PN','GTIN','Количество'],['',GTIN,300]]}))
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
 admin=browser.new_page(viewport={'width':1280,'height':900})
 pc=browser.new_page(viewport={'width':1280,'height':900})
 mobile=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
 errors=[]
 for page in [admin,pc,mobile]:
  page.on('pageerror',lambda e: errors.append(str(e)))
 admin.goto(BASE+'/admin')
 admin.locator('#admin-key').fill(os.environ['ADMIN_KEY']); admin.locator('#login button').click()
 expect(admin.locator('#workspace')).to_be_visible()
 expect(admin.locator('#token-panel')).to_be_visible()
 assert 'gis_admin_session' not in admin.evaluate('document.cookie')
 admin.locator('#token-uuid').fill('browser-uuid')
 admin.locator('#token-signature').fill('browser-signature')
 admin.locator('#token-submit').click()
 expect(admin.locator('#token-result')).to_have_text('Токен обновлён')
 admin.reload()
 expect(admin.locator('#workspace')).to_be_visible()
 expect(admin.locator('#login')).to_be_hidden()
 admin.locator('[data-kind=shipment]').click()

 admin.locator('#import-box summary').click(); admin.locator('#import-file').set_input_files(ship)
 admin.locator('#import-form button').click()
 expect(admin.locator('#doc-title')).to_contain_text('Браузер/001')
 assert admin.locator('#lines').inner_text().startswith('MOCK-PN-')
 admin.locator('#doc-settings summary').click()
 expect(admin.locator('[data-pn]')).to_be_visible()
 expect(admin.locator('[data-quantity]')).to_have_value('300')
 admin.locator('#doc-settings summary').click()
 doc=re.search(r'doc=([^&]+)',admin.url).group(1)
 admin.locator('#start').click();expect(admin.locator('#doc-status')).to_contain_text('В сборке')
 pc.goto(BASE+'/warehouse?kind=shipment')
 expect(pc.locator('#filter-count')).to_have_text('Документов: 1')
 pc.locator('#filter-org').select_option('0000000002');expect(pc.locator('#filter-count')).to_have_text('Документов: 0')
 pc.locator('#filter-org').select_option('0000000001');expect(pc.locator('#filter-count')).to_have_text('Документов: 1')
 pc.locator('#filter-scans').select_option('scanned');expect(pc.locator('#filter-count')).to_have_text('Документов: 0')
 pc.locator('#filter-scans').select_option('unscanned');expect(pc.locator('#filter-count')).to_have_text('Документов: 1')
 for page in [pc,mobile]:
  page.goto(BASE+'/warehouse?kind=shipment&doc='+doc)
  expect(page.locator('#wh-scan')).to_be_enabled()
 def scan(page,raw):
  page.locator('#wh-scan').fill(raw);page.locator('#wh-scan').press('Enter')
 scan(pc,code(100))
 expect(pc.locator('#doc-count')).to_contain_text('1 уникальных')
 expect(mobile.locator('#doc-count')).to_contain_text('1 уникальных',timeout=7000)
 expect(pc.locator('#lines')).to_contain_text('Не проверено')
 scan(mobile,code(100))
 expect(mobile.locator('#duplicate')).to_be_visible()
 mobile.get_by_role('button',name='Удалить повтор',exact=True).click()
 expect(mobile.locator('#doc-count')).to_contain_text('1 уникальных')
 scan(mobile,code(200))
 expect(pc.locator('#doc-count')).to_contain_text('2 уникальных',timeout=7000)
 pc.locator('#check').click()
 expect(pc.locator('#check-state')).to_have_text('Проверено. Документ полностью совпадает.')
 expect(pc.locator('#lines')).to_contain_text('300')
 scan(mobile,code(50))
 expect(pc.locator('#check-state')).to_contain_text('Данные предыдущей проверки',timeout=7000)
 expect(pc.locator('#complete')).to_be_disabled()
 bad_code=code(10,'ERR').replace('INTRODUCED','APPLIED')
 scan(mobile,bad_code)
 expect(pc.locator('#doc-count')).to_contain_text('4 уникальных',timeout=7000)
 pc.locator('#check').click();expect(pc.locator('#report-issue')).to_be_visible()
 pc.locator('#report-issue').click();pc.locator('#report-message').fill('Проверьте статус и лишнее количество')
 pc.locator('#report-form button').first.click()
 expect(pc.locator('#scan-feedback')).to_have_text('Сообщено ответственному')
 expect(admin.locator('#issue-state')).to_contain_text('открытых обращений: 1',timeout=7000)
 admin.locator('#show-issues').click();admin.locator('[data-issue]').first.click()
 expect(admin.locator('#issue-detail')).to_contain_text('APPLIEDQ10ERR')
 pc.locator('#scan-details summary').first.click()
 pc.locator('#scan-errors-only').check()
 expect(pc.locator('#scan-found')).to_have_text('Показано 1 из 4')
 pc.on('dialog',lambda d:d.accept())
 pc.locator('[data-delete]').click();expect(pc.locator('#doc-count')).to_contain_text('3 уникальных')
 pc.locator('#scan-errors-only').uncheck();pc.locator('#scan-query').fill('Q50')
 expect(pc.locator('#scan-found')).to_have_text('Показано 1 из 3')
 if not pc.locator('#scan-details').evaluate('(el)=>el.open'): pc.locator('#scan-details summary').first.click()
 pc.locator('[data-delete]').last.click()
 expect(pc.locator('#doc-count')).to_contain_text('2 уникальных')
 pc.locator('#scan-query').fill('')
 admin.locator('#issues-refresh').click()
 expect(admin.locator('#issue-events')).to_contain_text('КМ удалён')
 expect(admin.locator('#issue-detail')).to_contain_text('APPLIEDQ10ERR')
 admin.locator('#issue-resolution').fill('Заменили ошибочный КМ и убрали лишнее')
 admin.locator('#return-warehouse').click()
 expect(admin.locator('#notice')).to_have_text('Документ возвращён на склад с комментарием')
 expect(admin.locator('#issue-state')).to_have_text('Обращения разобраны')
 expect(mobile.locator('#warehouse-replies')).to_contain_text('Заменили ошибочный КМ и убрали лишнее',timeout=7000)
 expect(mobile.locator('#wh-scan')).to_be_enabled()
 expect(pc.locator('#warehouse-replies')).to_contain_text('Заменили ошибочный КМ и убрали лишнее',timeout=7000)
 mobile.screenshot(path=str(TEST_DIR / 'mobile-reply.png'),full_page=True)
 admin.screenshot(path=str(TEST_DIR / 'admin-reply.png'),full_page=True)
 pc.locator('#back').click();pc.locator('#filter-scans').select_option('scanned')
 expect(pc.locator('#document-list')).to_contain_text('Ответственный ответил — можно продолжать')
 pc.locator('[data-doc="'+doc+'"]').click()
 pc.locator('[data-ack-return]').click()
 expect(pc.locator('#warehouse-replies')).to_be_empty()
 expect(mobile.locator('#warehouse-replies')).to_be_empty(timeout=7000)
 expect(admin.locator('#issue-detail')).to_contain_text('Склад ознакомился',timeout=7000)
 # Theme setting and manual/scanner input remain available on a narrow screen.
 mobile.locator('#theme-btn').click()
 expect(mobile.locator('html')).to_have_attribute('data-theme','dark')
 mobile.locator('#manual-input').click();expect(mobile.locator('#wh-scan')).to_have_attribute('inputmode','text')
 mobile.locator('#manual-input').click();expect(mobile.locator('#wh-scan')).to_have_attribute('inputmode','none')
 mobile.screenshot(path=str(TEST_DIR / 'mobile-dark.png'),full_page=True)
 assert mobile.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile page overflows horizontally'
 pc.locator('#check').click();expect(pc.locator('#complete')).to_be_enabled()
 pc.screenshot(path=str(TEST_DIR / 'desktop.png'),full_page=True)
 mobile.screenshot(path=str(TEST_DIR / 'mobile.png'),full_page=True)
 pc.locator('#complete').click();expect(pc.locator('#doc-status')).to_contain_text('Собран')
 expect(mobile.locator('#wh-scan')).to_be_disabled(timeout=7000)
 expect(admin.locator('#export')).to_be_visible(timeout=7000)
 with admin.expect_download() as download:
  admin.locator('#export').click()
 assert 'КМ.xlsx' in download.value.suggested_filename
 admin.locator('#back').click();admin.locator('#filter-status').select_option('draft')
 expect(admin.locator('#filter-count')).to_have_text('Документов: 0')
 admin.locator('#filter-status').select_option('completed');expect(admin.locator('#filter-count')).to_have_text('Документов: 1')
 admin.locator('#filter-query').fill('НетТакого');expect(admin.locator('#filter-count')).to_have_text('Документов: 0')
 admin.locator('#filter-query').fill('БРАУЗЕР');expect(admin.locator('#filter-count')).to_have_text('Документов: 1')
 admin.locator('#filter-issues').select_option('closed');expect(admin.locator('#filter-count')).to_have_text('Документов: 1')
 admin.locator('#filter-from').fill('2026-10-22');expect(admin.locator('#filter-count')).to_have_text('Документов: 0')
 admin.locator('#filter-from').fill('2026-10-21');admin.locator('#filter-to').fill('2026-10-21');expect(admin.locator('#filter-count')).to_have_text('Документов: 1')
 admin.locator('#document-filters button[type=reset]').click()
 admin.screenshot(path=str(TEST_DIR / 'admin-list.png'),full_page=True)
 # Receipt: a separate browser resumes a previously started operation.
 receipt=str(TEST_DIR / 'receipt.xlsx')
 open(receipt,'wb').write(make_xlsx({'Документ':header[:4],'Товары':[['PN','GTIN','Код маркировки','Количество в КМ'],['PN',GTIN,code(100,'R'),100]]}))
 admin.locator('[data-kind=receipt]').click();admin.locator('#import-box summary').click() if not admin.locator('#import-box').evaluate('(el)=>el.open') else None
 admin.locator('#import-file').set_input_files(receipt);admin.locator('#import-form button').click()
 expect(admin.locator('#doc-title')).to_contain_text('Приёмка')
 receipt_id=re.search(r'doc=([^&]+)',admin.url).group(1)
 admin.locator('#start').click();expect(admin.locator('#doc-status')).to_contain_text('В приёмке')
 mobile.goto(BASE+'/warehouse?kind=receipt&doc='+receipt_id)
 expect(mobile.locator('#wh-scan')).to_be_enabled()
 scan(mobile,code(100,'R'));expect(mobile.locator('#doc-count')).to_contain_text('1 уникальных')
 mobile.reload();expect(mobile.locator('#doc-count')).to_contain_text('1 уникальных')
 mobile.locator('#check').click();expect(mobile.locator('#complete')).to_be_enabled()
 mobile.locator('#complete').click();expect(mobile.locator('#doc-status')).to_contain_text('Принят')
 # Delete an active document; scanner on another device must stop.
 admin.locator('[data-kind=shipment]').click()
 if not admin.locator('#import-box').evaluate('(el)=>el.open'): admin.locator('#import-box summary').click()
 admin.locator('#import-file').set_input_files(ship);admin.locator('#import-form button').click()
 expect(admin.locator('#doc-title')).to_contain_text('Отгрузка')
 delete_id=re.search(r'doc=([^&]+)',admin.url).group(1)
 admin.locator('#start').click();expect(admin.locator('#doc-status')).to_contain_text('В сборке')
 mobile.goto(BASE+'/warehouse?kind=shipment&doc='+delete_id)
 expect(mobile.locator('#wh-scan')).to_be_enabled()
 scan(mobile,code(50,'RESTORE'))
 expect(admin.locator('#doc-count')).to_contain_text('1 уникальных',timeout=7000)
 admin.locator('#delete-document').click()
 expect(admin.locator('#delete-dialog')).to_be_visible()
 admin.locator('#delete-number').fill('wrong')
 expect(admin.locator('#delete-confirm')).to_be_disabled()
 admin.locator('#delete-number').fill('Браузер/001')
 admin.locator('#delete-confirm').click()
 expect(admin.locator('#notice')).to_have_text('Документ удалён')
 expect(mobile.locator('#wh-scan')).to_be_disabled(timeout=7000)
 expect(mobile.locator('#scan-feedback')).to_contain_text('Документ удалён')
 admin.locator('#filter-trash').select_option('true')
 expect(admin.locator('#filter-count')).to_have_text('Документов: 1')
 admin.locator('[data-doc="'+delete_id+'"]').click()
 expect(admin.locator('#restore-document')).to_be_visible()
 expect(admin.locator('#save')).to_be_disabled()
 admin.locator('#restore-document').click()
 expect(admin.locator('#notice')).to_have_text('Документ восстановлен из корзины')
 expect(mobile.locator('#wh-scan')).to_be_enabled(timeout=7000)
 expect(mobile.locator('#doc-count')).to_contain_text('1 уникальных')
 admin.locator('#token-tab').click();expect(admin.locator('#token-panel')).to_be_visible()
 admin.locator('#logout').click();expect(admin.locator('#login')).to_be_visible()
 admin.reload();expect(admin.locator('#login')).to_be_visible()
 pc.goto(BASE+'/');pc.locator('#scan-input').fill(code(100));pc.locator('#scan-input').press('Enter')
 expect(pc.locator('#scan-result')).to_contain_text('В обороте')
 mobile.goto(BASE+'/');expect(mobile.locator('html')).to_have_attribute('data-theme','dark')
 mobile.locator('#theme-btn').click();expect(mobile.locator('html')).to_have_attribute('data-theme','light')
 mobile.screenshot(path=str(TEST_DIR / 'verify-light.png'),full_page=True)
 assert not errors, errors
 print('Browser PASS: return to warehouse/ack, shared theme, responsive UI, manual input, filters, incident snapshot/log/resolve, code search/error filtering, trash restore, unified admin, session reload/logout, token update, document deletion, admin import/PN/start/export, 2 independent clients, mobile layout, duplicate, stale result, deletion, completion freeze, receipt after reload, general scan. No JS errors.')
 browser.close()
