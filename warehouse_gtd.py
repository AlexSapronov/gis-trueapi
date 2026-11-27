"""FIFO customs-declaration rows attached to bulk warehouse shipments."""
from __future__ import annotations

import io
import zipfile
from collections import defaultdict

import openpyxl

from warehouse_excel import MAX_FILE_SIZE, positive_integer
from warehouse_store import WarehouseError


REQUIRED = {'Number', 'GoodId', 'Amount', 'Declaration'}
OPTIONAL = {'FromINN', 'ToINN', 'GoodName'}


def identifier(value, label):
    if value is None or isinstance(value, bool):
        raise ValueError(label + ': значение не заполнено')
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(label + ': ожидается целое значение')
        value = str(int(value))
    value = str(value).strip()
    if not value:
        raise ValueError(label + ': значение не заполнено')
    return value


def parse_gtd_file(content: bytes) -> list[dict]:
    if not content or len(content) > MAX_FILE_SIZE:
        raise WarehouseError('Файл разбивки ГТД должен быть XLSX размером до 10 МБ')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 80 * 1024 * 1024:
                raise WarehouseError('Слишком большой распакованный файл разбивки ГТД')
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except WarehouseError:
        raise
    except Exception as exc:
        raise WarehouseError('Не удалось прочитать XLSX с разбивкой ГТД') from exc
    result = []
    try:
        for sheet in book:
            header_row = None
            columns = None
            for row in sheet.iter_rows(min_row=1, max_row=min(20, sheet.max_row)):
                names = {str(cell.value).strip(): i for i, cell in enumerate(row) if cell.value is not None}
                if REQUIRED <= names.keys():
                    header_row, columns = row[0].row, names
                    break
            if header_row is None:
                continue
            if sheet.max_row > 10001 or sheet.max_column > 200:
                raise WarehouseError('Файл разбивки ГТД: не более 10000 строк и 200 столбцов на лист')
            for number in range(header_row + 1, sheet.max_row + 1):
                cells = {key: sheet.cell(number, index + 1) for key, index in columns.items()}
                if all(cell.value is None for cell in cells.values()):
                    continue
                try:
                    if any(cells[key].data_type == 'f' for key in REQUIRED):
                        raise ValueError('Формулы в ключевых полях не поддерживаются')
                    invoice_number = identifier(cells['Number'].value, 'Number')
                    good_id = identifier(cells['GoodId'].value, 'GoodId')
                    amount = positive_integer(cells['Amount'].value)
                    declaration = identifier(cells['Declaration'].value, 'Declaration')
                    from_inn = identifier(cells['FromINN'].value, 'FromINN') if cells.get('FromINN') and cells['FromINN'].value is not None else ''
                    to_inn = identifier(cells['ToINN'].value, 'ToINN') if cells.get('ToINN') and cells['ToINN'].value is not None else ''
                    result.append(dict(number=invoice_number, good_id=good_id, quantity=amount,
                                        declaration=declaration, from_inn=from_inn, to_inn=to_inn,
                                        source_sheet=sheet.title, source_row=number, position=len(result) + 1))
                except (ValueError, WarehouseError) as exc:
                    raise WarehouseError(f'Разбивка ГТД, лист «{sheet.title}», строка {number}: {exc}') from exc
        if not result:
            raise WarehouseError('Не найдены столбцы Number, GoodId, Amount и Declaration')
        return result
    finally:
        book.close()


def match_gtd_rows(group: dict, rows: list[dict], *, ambiguous_number=False, ambiguous_party=False):
    """Match by invoice Number + GoodId; optional INNs narrow duplicate invoice numbers."""
    all_source = defaultdict(list)
    for item in group['lines']:
        all_source[item['good_id']].append(item)
    source = {good_id: items for good_id, items in all_source.items()
              if any(item['gtin'] for item in items)}
    candidates = []
    for row in rows:
        if row['number'] != group['number']:
            continue
        if row['from_inn'] and row['from_inn'] != group['org_inn']:
            continue
        if row['to_inn'] and row['to_inn'] != group['recipient_inn']:
            continue
        candidates.append(row)
    if not candidates:
        return dict(state='pending', rows=[],
                    message='Ожидайте загрузки данных по ГТД. Груз пока не упаковывать. Для этой накладной пока нет строк в файле.')
    if ambiguous_party:
        return dict(state='invalid', rows=[],
                    message='В массовой загрузке есть несколько накладных с одинаковыми Number и ИНН сторон; сопоставление ГТД неоднозначно')
    if ambiguous_number and not all(r['from_inn'] == group['org_inn'] and r['to_inn'] == group['recipient_inn'] for r in candidates):
        return dict(state='invalid', rows=[],
                    message='Номер накладной встречается в массовой загрузке несколько раз; для точного сопоставления добавьте FromINN и ToINN в файл ГТД')
    by_good = defaultdict(list)
    for row in candidates:
        by_good[row['good_id']].append(row)
    matched = []
    for good_id, items in source.items():
        gtins = {item['gtin'] for item in items}
        if len(gtins) != 1 or not next(iter(gtins)):
            return dict(state='invalid', rows=[],
                        message=f'GoodId {good_id}: невозможно однозначно определить GTIN для строк ГТД')
        selected = by_good.get(good_id, [])
        if not selected:
            return dict(state='invalid', rows=[],
                        message=f'В файле ГТД отсутствуют строки для GoodId {good_id}')
        expected = sum(item['quantity'] for item in items)
        actual = sum(row['quantity'] for row in selected)
        if expected != actual:
            return dict(state='invalid', rows=[],
                        message=f'GoodId {good_id}: в массовой загрузке {expected} шт., в разбивке ГТД {actual} шт.')
        gtin = next(iter(gtins))
        for row in selected:
            matched.append(dict(good_id=good_id, gtin=gtin, quantity=row['quantity'],
                                declaration=row['declaration'], source_sheet=row['source_sheet'],
                                source_row=row['source_row'], position=row['position']))
    extra = set(by_good) - set(all_source)
    if extra:
        return dict(state='invalid', rows=[],
                    message='В файле ГТД есть лишние GoodId: ' + ', '.join(sorted(extra)[:10]))
    matched.sort(key=lambda item: item['position'])
    return dict(state='ready', rows=matched, message='Разбивка ГТД загружена; ожидается сверка сканов')
