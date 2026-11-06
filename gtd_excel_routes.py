"""Background Excel checks share the existing administrator-only router."""
import asyncio
from pathlib import PurePath
from urllib.parse import quote

from fastapi import File, Form, UploadFile
from fastapi.responses import Response

from km_report import read_codes, process_report
from gtd_excel import load_book, read_rows, read_catalog, process_excel_job
from warehouse_excel import MAX_FILE_SIZE, make_xlsx
from warehouse_store import WarehouseError


def install_gtd_excel(app, admin, service_getter, store_getter):
    tasks=set()

    @app.on_event('startup')
    async def recover_excel_jobs():
        store_getter().gtd_excel_recover()

    @app.on_event('shutdown')
    async def stop_excel_jobs():
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)

    @admin.get('/km-report/template')
    def km_report_template():
        return Response(make_xlsx({'Коды': [['КМ']]}),
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': 'attachment; filename=KM.xlsx'})

    @admin.post('/km-report')
    async def upload_km_report(file: UploadFile = File(...), org_inn: str = Form(...)):
        from organizations import registry
        if not registry.has(org_inn):
            raise WarehouseError('Выберите нашу организацию')
        if not (file.filename or '').lower().endswith('.xlsx'):
            raise WarehouseError('Поддерживается файл .xlsx')
        content = await file.read(MAX_FILE_SIZE + 1)
        await asyncio.to_thread(read_codes, content)
        store = store_getter()
        filename = PurePath((file.filename or 'КМ.xlsx').replace('\\', '/')).name[:200]
        job_id = store.gtd_excel_create(filename, content)
        task = asyncio.create_task(process_report(store, service_getter(), job_id, org_inn))
        tasks.add(task); task.add_done_callback(tasks.discard)
        return store.gtd_excel_get(job_id)

    @admin.get('/km-report/{job_id}')
    def km_report_status(job_id: str):
        return store_getter().gtd_excel_get(job_id)

    @admin.get('/km-report/{job_id}/download')
    def km_report_download(job_id: str):
        job = store_getter().gtd_excel_download(job_id)
        if (job['report'] or {}).get('kind') != 'km-report':
            raise WarehouseError('Отчёт КМ не найден', 404)
        return Response(job['output'], media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote('КМ_количество_ГТД.xlsx'),
                                 'Cache-Control': 'no-store'})

    @admin.get('/gtd-catalog')
    def gtd_catalog():
        return store_getter().gtd_catalog()

    @admin.get('/gtd-catalog/template')
    def gtd_catalog_template():
        return Response(make_xlsx({'Справочник':[['GoodID','GTIN']]}),
                        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition':"attachment; filename=GoodID_GTIN.xlsx"})

    @admin.post('/gtd-catalog')
    async def upload_catalog(file:UploadFile=File(...)):
        content=await file.read(MAX_FILE_SIZE+1)
        mapping=await asyncio.to_thread(read_catalog,content)
        return store_getter().gtd_catalog_save(mapping)

    @admin.post('/gtd-excel')
    async def upload_gtd_excel(file:UploadFile=File(...)):
        if not (file.filename or '').lower().endswith('.xlsx'):
            raise WarehouseError('Поддерживается файл .xlsx')
        content=await file.read(MAX_FILE_SIZE+1)
        # Reject unreadable or incompatible inputs before scheduling any API work.
        book=await asyncio.to_thread(load_book,content)
        await asyncio.to_thread(read_rows,book)
        book.close()
        filename=PurePath((file.filename or 'ГТД.xlsx').replace('\\','/')).name[:200]
        store=store_getter()
        job_id=store.gtd_excel_create(filename,content)
        task=asyncio.create_task(process_excel_job(store,service_getter(),job_id))
        tasks.add(task);task.add_done_callback(tasks.discard)
        return store.gtd_excel_get(job_id)

    @admin.get('/gtd-excel/{job_id}')
    def gtd_excel_status(job_id:str):
        return store_getter().gtd_excel_get(job_id)

    @admin.get('/gtd-excel/{job_id}/download')
    def gtd_excel_download(job_id:str):
        job=store_getter().gtd_excel_download(job_id)
        filename=PurePath(job['filename']).stem+'_ГТД_проверены.xlsx'
        return Response(job['output'],media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(filename),
                                 'Cache-Control':'no-store'})
