"""Read complete KMs and export quantityInPack and customs data from True API."""
import asyncio

from gtd_excel import load_book
from models import AppError
from warehouse_excel import MAX_ROWS, make_xlsx, read_excel_strings, scan_data, text_cell
from warehouse_store import WarehouseError

HEADERS = {'км', 'код маркировки', 'datamatrix', 'код идентификации', 'ки', 'cis', 'полный код маркировки'}
OUTPUT = ['Лист', 'Строка', 'КМ', 'КИ', 'GTIN', 'Количество в упаковке (quantityInPack)', 'Номер ГТД', 'Статус ЧЗ', 'Пояснение']


def read_codes(content):
    book = load_book(content)
    try:
        strings = read_excel_strings(content)
        rows = []
        for sheet in book:
            if sheet.max_column > 200 or sheet.max_row > MAX_ROWS + 20:
                raise WarehouseError('Поддерживается до 50000 строк и 200 столбцов на листе')
            header = None
            for cells in sheet.iter_rows(max_row=min(20, sheet.max_row)):
                columns = [cell.column for cell in cells if str(cell.value or '').strip().casefold() in HEADERS]
                if len(columns) > 1:
                    raise WarehouseError(f'Лист «{sheet.title}»: оставьте один столбец с КМ')
                if columns:
                    header, column = cells[0].row, columns[0]
                    break
            if header is None:
                continue
            for cells in sheet.iter_rows(min_row=header + 1, min_col=column, max_col=column):
                cell = cells[0]
                if cell.value is None:
                    continue
                row = dict(sheet=sheet.title, row=cell.row, raw='', cis='', gtin='', reason='')
                try:
                    row['raw'] = text_cell(cell, 'Код маркировки', strings=strings[sheet.title])
                    data = scan_data(row['raw'])
                    row['cis'] = '01' + data['gtin'] + '21' + data['cis'][14:]
                    row['gtin'] = data['gtin']
                except (ValueError, WarehouseError) as exc:
                    row['reason'] = str(exc)
                rows.append(row)
                if len(rows) > MAX_ROWS:
                    raise WarehouseError('Поддерживается до 50000 КМ в одном файле')
        if not rows:
            raise WarehouseError('Не найдены КМ. Нужен столбец «КМ», «Код маркировки» или «DataMatrix» с заголовком в первых 20 строках')
        return rows
    finally:
        book.close()


async def process_report(store, service, job_id, org_inn):
    try:
        job = store.gtd_excel_get(job_id, files=True)
        rows = await asyncio.to_thread(read_codes, job['source'])
        cises = list(dict.fromkeys(row['cis'] for row in rows if row['cis']))
        results = {}
        store.gtd_excel_progress(job_id, 0, len(cises))
        for start in range(0, len(cises), 100):
            batch = cises[start:start + 100]
            try:
                results.update(await service.lookup_km_report_batch(batch, org_inn))
            except AppError as exc:
                # Identify every row whose API lookup failed.
                results.update({cis: dict(message='Ошибка запроса ЧЗ: ' + exc.message) for cis in batch})
            store.gtd_excel_progress(job_id, min(start + 100, len(cises)), len(cises))
        output = [OUTPUT]
        for row in rows:
            info = results.get(row['cis'], {})
            reason = '; '.join(filter(None, [row['reason'], info.get('message')]))
            output.append([row['sheet'], row['row'], row['raw'], row['cis'], row['gtin'], info.get('quantity_in_pack'),
                           info.get('declaration_number'), info.get('status'), reason])
        content = await asyncio.to_thread(make_xlsx, {'КМ и ГТД': output})
        report = dict(kind='km-report', total=len(rows), unique=len(cises),
                      with_quantity=sum(results.get(row['cis'], {}).get('quantity_in_pack') is not None for row in rows),
                      with_gtd=sum(bool(results.get(row['cis'], {}).get('declaration_number')) for row in rows))
        store.gtd_excel_finish(job_id, content, report, {})
    except asyncio.CancelledError:
        store.gtd_excel_fail(job_id, 'Обработка прервана. Загрузите файл повторно.')
        raise
    except Exception:
        store.gtd_excel_fail(job_id, 'Не удалось сформировать отчёт. Загрузите файл повторно.')
