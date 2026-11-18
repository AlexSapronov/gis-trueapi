"""Import the unmodified Flarp invoice list; synchronize shipment tasks by InvoiceId."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import openpyxl

from organizations import registry
from warehouse_excel import MAX_FILE_SIZE, MAX_ROWS, positive_integer, shipment_gtin_cell
from warehouse_store import WarehouseError

BASE_COLUMNS = ('InvoiceId', 'GoodId', 'Наименование', 'Количество шт.',
            'Номер накладной', 'GTIN', 'Требуется маркировка')
REQUIRED = BASE_COLUMNS + ('ИНН отправителя', 'ИНН получателя')


def open_book(content):
    if len(content) > MAX_FILE_SIZE:
        raise WarehouseError('Размер файла превышает 10 МБ')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 80 * 1024 * 1024:
                raise WarehouseError('Слишком большой распакованный Excel')
        book = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    except WarehouseError:
        raise
    except Exception as exc:
        raise WarehouseError('Не удалось прочитать XLSX-файл') from exc
    return book


def inn_text(value):
    # Client tax identifiers are opaque: foreign identifiers need not use Russian INN length.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('Заполните ИНН / налоговый идентификатор')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError('ИНН должен быть целым числом')
        value = str(int(value))
    inn = str(value or '').strip()
    if not inn:
        raise ValueError('Заполните ИНН / налоговый идентификатор')
    return inn


def parse_bulk(content):
    book = open_book(content)
    groups = {}
    try:
        if len(book.sheetnames) != 1:
            raise WarehouseError('Ожидается однолистная выгрузка списка накладных')
        sheet = book.active
        headers = next(sheet.iter_rows(min_row=1, max_row=1), ())
        heads = [str(c.value or '').strip() for c in headers]
        missing = [name for name in REQUIRED if name not in heads]
        if missing:
            raise WarehouseError('Выгрузите список накладных с обязательными столбцами. Не найдены: ' + ', '.join(missing))
        columns = REQUIRED + ('Получатель',) if 'Получатель' in heads else REQUIRED
        repeated = [name for name in columns if heads.count(name) > 1]
        if repeated:
            raise WarehouseError('В выгрузке повторяются столбцы: ' + ', '.join(repeated))
        indices = {name: heads.index(name) for name in columns}
        for n, row in enumerate(sheet.iter_rows(min_row=2), 2):
            if n > MAX_ROWS + 1:
                raise WarehouseError('В файле больше 50000 строк')
            if all(c.value is None for c in row):
                continue
            cells = {name: row[i] for name, i in indices.items()}
            try:
                invoice_id = str(positive_integer(cells['InvoiceId'].value))
                if cells['InvoiceId'].data_type == 'f':
                    raise ValueError('InvoiceId: формулы не допускаются')
            except ValueError as exc:
                # An unidentifiable row cannot safely be excluded from any invoice.
                raise WarehouseError(f'Строка {n}: {exc}') from exc
            group = groups.setdefault(invoice_id, dict(invoice_id=invoice_id, number='', lines=[], errors=[],
                                                      org_inn='', recipient_inn='', recipient_name=''))
            try:
                if any(c.data_type == 'f' for c in cells.values()):
                    raise ValueError('Формулы в используемых столбцах не допускаются')
                number = str(cells['Номер накладной'].value or '').strip()
                if not number or len(number) > 300:
                    raise ValueError('Не заполнен номер накладной или он длиннее 300 символов')
                if group['number'] and group['number'] != number:
                    raise ValueError('У одного InvoiceId разные номера накладной')
                group['number'] = number
                for column, field in (('ИНН отправителя', 'org_inn'), ('ИНН получателя', 'recipient_inn')):
                    try:
                        inn = inn_text(cells[column].value)
                    except ValueError as exc:
                        raise ValueError(f'{column}: {exc}') from exc
                    if group[field] and group[field] != inn:
                        raise ValueError(f'{column}: разные значения в одной накладной')
                    group[field] = inn
                if 'Получатель' in cells and not group['recipient_name']:
                    group['recipient_name'] = str(cells['Получатель'].value or '').strip()
                good_id = str(positive_integer(cells['GoodId'].value))
                pn = str(cells['Наименование'].value or '').strip()
                if not pn or len(pn) > 1000:
                    raise ValueError('Не заполнено наименование или оно длиннее 1000 символов')
                qty = positive_integer(cells['Количество шт.'].value)
                flag = str(cells['Требуется маркировка'].value or '').strip().casefold()
                gtin = '' if flag == '??' else shipment_gtin_cell(cells['GTIN'])
                if flag not in ('да', 'нет', '??'):
                    raise ValueError('Не определена маркировка (ожидается Да/Нет/??)')
                if flag == 'да' and not gtin:
                    raise ValueError('Для маркируемой позиции отсутствует GTIN')
                if flag == 'нет' and gtin:
                    raise ValueError('GTIN указан при признаке маркировки Нет')
                group['lines'].append(dict(good_id=good_id, gtin=gtin, pn=pn,
                                           pn_source='excel', quantity=qty))
            except ValueError as exc:
                group['errors'].append(f'Строка {n}: {exc}')
        if not groups:
            raise WarehouseError('В файле нет накладных')
        return list(groups.values())
    finally:
        book.close()


def import_bulk(store, content):
    groups = parse_bulk(content)
    report = []
    for group in groups:
        base = dict(invoice_id=group['invoice_id'], number=group['number'])
        if group['errors']:
            report.append({**base, 'status': 'error', 'message': '; '.join(group['errors'])})
            continue
        # Consolidate by GTIN for the existing scanner; preserve distinct unmarked goods.
        merged = {}
        for line in group['lines']:
            key = line['gtin'] or 'good:' + line['good_id']
            if key in merged and merged[key]['pn'] != line['pn']:
                group['errors'].append('У одного GTIN/GoodId разные наименования')
            elif key in merged:
                merged[key]['quantity'] += line['quantity']
                if merged[key]['quantity'] > 2**31 - 1:
                    group['errors'].append('Суммарное количество превышает 2147483647')
            else:
                merged[key] = dict(line)
        if group['errors']:
            report.append({**base, 'status': 'error', 'message': '; '.join(group['errors'])})
            continue
        org = registry.get(group['org_inn'])
        if not org:
            report.append({**base, 'status': 'error', 'message': 'ИНН отправителя отсутствует в наших организациях'})
            continue
        header = dict(kind='shipment', number=group['number'],
                      date=datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat(),
                      org_inn=org.inn, org_name=org.name, recipient_inn=group['recipient_inn'],
                      recipient_name=group['recipient_name'])
        try:
            result = store.sync_shipment_invoice(group['invoice_id'], header, list(merged.values()))
            report.append({**base, **result})
        except WarehouseError as exc:
            report.append({**base, 'status': 'error', 'message': exc.message})
    return dict(results=report, counts={key: sum(r['status'] == key for r in report)
                                       for key in ('created', 'updated', 'unchanged', 'skipped', 'error')})
