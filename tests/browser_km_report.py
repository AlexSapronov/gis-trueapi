"""Optional browser check: python tests/browser_km_report.py (Playwright + Chromium)."""
import io
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
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

if '--server' in sys.argv:
    import main
    from test_km_report import Api, ORG
    api = Api()
    main.svc.client.info = api.info
    main.svc.client.short_info = api.short_info
    main.svc.tokens.set(ORG, 'synthetic-token')
    import uvicorn
    uvicorn.run(main.app, host='127.0.0.1', port=int(os.environ['KM_TEST_PORT']), log_level='warning')
    sys.exit()

import openpyxl
from playwright.sync_api import sync_playwright, expect
from test_km_report import workbook, DT

with tempfile.TemporaryDirectory(prefix='km-report-browser-') as directory:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'GIS_MODE': 'mock', 'ADMIN_KEY': 'km-browser-key',
           'ORGANIZATIONS': '0000000001=Тест', 'KM_TEST_PORT': str(port),
           'WAREHOUSE_DB': str(Path(directory, 'warehouse.db')),
           'TOKENS_FILE': str(Path(directory, 'tokens.json'))}
    server = subprocess.Popen([sys.executable, __file__, '--server'], cwd=ROOT, env=env)
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + '/api/status', timeout=1)
                break
            except OSError:
                time.sleep(.1)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'], executable_path=shutil.which('chromium'))
            page = browser.new_page(viewport={'width': 1280, 'height': 1000})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base + '/admin?section=gtd')
            page.locator('#admin-key').fill('km-browser-key')
            page.locator('#login button').click()
            expect(page.locator('#km-report-form')).to_be_visible()
            page.locator('#km-report-file').set_input_files({'name': 'codes.xlsx', 'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'buffer': workbook()})
            page.locator('#km-report-form button').click()
            expect(page.locator('#km-report-download')).to_be_visible(timeout=10000)
            expect(page.locator('#km-report-status')).to_contain_text('с количеством из ЧЗ: 4')
            with page.expect_download() as downloaded:
                page.locator('#km-report-download').click()
            sheet = openpyxl.load_workbook(io.BytesIO(Path(downloaded.value.path()).read_bytes())).active
            assert sheet['F2'].value == 6000 and sheet['G2'].value == DT
            page.reload()
            expect(page.locator('#km-report-download')).to_be_visible(timeout=10000)
            page.locator('#gtd-excel-tab').click()
            expect(page.locator('#gtd-excel-panel')).to_be_visible()
            page.locator('#gtd-tab').click()
            expect(page.locator('#km-report-download')).to_be_visible()
            assert not errors, errors
            browser.close()
        print('KM report browser workflow passed: upload, API quantity, download, reload, navigation')
    finally:
        server.terminate()
        server.wait(timeout=5)
