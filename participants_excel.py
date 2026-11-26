"""Mass participant registration / radio product-group check."""
import asyncio
import io
import re
from copy import copy

import httpx
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter

from gtd_excel import load_book
from warehouse_excel import MAX_ROWS
from warehouse_store import WarehouseError

BATCH_SIZE = 100
HEADERS = ("Зарегистрирован", "Подключен РЭП")
UNKNOWN = "Не проверено"
INVALID = "Некорректный ИНН"
MOCK = "Тестовый режим"
COLORS = {"Да": "D9EAD3", "Нет": "F4CCCC", "Да (заблокирован)": "FFF2CC"}


def normalize_inn(cell):
    value = cell.value
    if cell.data_type == "f" or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            return ""
        digits = str(int(value))
        # A numeric cell can retain leading zeroes in an explicit Excel mask.
        mask = cell.number_format
        if re.fullmatch(r"0{10}|0{12}", mask):
            digits = digits.zfill(len(mask))
    elif isinstance(value, str):
        digits = value.strip()
    else:
        return ""
    return digits if re.fullmatch(r"[0-9]{10}|[0-9]{12}", digits) else ""


def read_rows(book):
    """Locate input columns and reuse existing output headers on repeat checks."""
    rows = []
    for sheet in book.worksheets:
        if sheet.max_column > 200 or sheet.max_row > MAX_ROWS + 20:
            raise WarehouseError("Поддерживается до 50000 строк и 200 столбцов на листе")
        for cells in sheet.iter_rows(max_row=min(20, sheet.max_row)):
            names = {}
            for cell in cells:
                name = str(cell.value or "").strip().casefold()
                names.setdefault(name, []).append(cell.column)
            if not {"полное наименование", "инн"} <= names.keys():
                continue
            if any(len(names.get(name, [])) > 1 for name in
                   ("полное наименование", "инн", *(h.casefold() for h in HEADERS))):
                raise WarehouseError(f"Лист «{sheet.title}»: дублирующиеся заголовки")
            header = cells[0].row
            name_col, inn_col = names["полное наименование"][0], names["инн"][0]
            next_col = sheet.max_column + 1
            out = []
            for title in HEADERS:
                if title.casefold() in names:
                    col = names[title.casefold()][0]
                else:
                    col = next_col
                    next_col += 1
                out.append(col)
            if max(out) > 16384:
                raise WarehouseError("Нет места для столбцов результата")
            for row in range(header + 1, sheet.max_row + 1):
                name_cell, inn_cell = sheet.cell(row, name_col), sheet.cell(row, inn_col)
                if name_cell.value is None and inn_cell.value is None:
                    continue
                rows.append((sheet.title, header, row, tuple(out), normalize_inn(inn_cell)))
                if len(rows) > MAX_ROWS:
                    raise WarehouseError("Поддерживается до 50000 контрагентов в одном файле")
            break
    if not rows:
        raise WarehouseError("Не найдены строки с заголовками «Полное наименование» и «ИНН» в первых 20 строках")
    return rows


def validate_workbook(content):
    book = load_book(content)
    try:
        read_rows(book)
    finally:
        book.close()


def classify(record):
    """Never turn missing data or a transport error into a negative result."""
    error = str(record.get("error_code", ""))
    if error == "404":
        return "Нет", "Нет"
    if error == "400":
        return INVALID, INVALID
    if error or record.get("error_message"):
        return UNKNOWN, UNKNOWN
    registered = record.get("is_registered")
    if type(registered) is not bool:
        return UNKNOWN, UNKNOWN
    blocked = record.get("statusInn") == "BLOCKED" or str(record.get("status", "")).strip().casefold() in {
        "заблокирован", "blocked"}
    reg = ("Да (заблокирован)" if blocked else "Да") if registered else "Нет"
    groups = record.get("productGroups")
    if isinstance(groups, list) and all(isinstance(g, str) for g in groups):
        radio = "Да" if "radio" in groups else "Нет"
    else:
        radio = UNKNOWN
    return reg, radio


async def lookup_batch(client, inns):
    """Public endpoint; the existing live client's configured host and HTTP pool."""
    response = await client.http.get(
        client.base_url + "/api/v3/true-api/participants",
        params=[("inns", inn) for inn in inns],
        headers={"Accept": "application/json"},
    )
    if response.status_code not in (200, 400, 404):
        raise ValueError("Ошибка HTTP True API")
    data = response.json()
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("Неожиданный ответ True API")
    result = {}
    seen = set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("inn"), str):
            continue
        inn = item["inn"]
        if inn not in inns:
            continue
        if inn in seen:
            result[inn] = (UNKNOWN, UNKNOWN)
            continue
        seen.add(inn)
        if response.status_code != 200 and str(item.get("error_code")) not in ("400", "404"):
            continue
        result[inn] = classify(item)
    return result


def render_workbook(book, rows, results):
    initialized = set()
    summary = dict(kind="participants-excel", total=len(rows), unique=len(results),
                   registered=0, radio=0, blocked=0, unknown=0, invalid=0, mock=0)
    for sheet_name, header, row, cols, inn in rows:
        sheet = book[sheet_name]
        if sheet_name not in initialized:
            for col, title in zip(cols, HEADERS):
                cell = sheet.cell(header, col, title)
                cell._style = copy(sheet.cell(header, 1)._style)
                sheet.column_dimensions[get_column_letter(col)].width = 25
            initialized.add(sheet_name)
        reg, radio = results.get(inn, (UNKNOWN, UNKNOWN)) if inn else (INVALID, INVALID)
        for col, value in zip(cols, (reg, radio)):
            cell = sheet.cell(row, col)
            cell.value = value
            cell.fill = PatternFill("solid", fgColor=COLORS.get(value, "FFF2CC"))
        summary["registered"] += reg in ("Да", "Да (заблокирован)")
        summary["blocked"] += reg == "Да (заблокирован)"
        summary["radio"] += radio == "Да"
        summary["unknown"] += UNKNOWN in (reg, radio)
        summary["invalid"] += INVALID in (reg, radio)
        summary["mock"] += MOCK in (reg, radio)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue(), summary


async def process_participants(store, service, job_id, mock=False):
    book = None
    try:
        job = store.gtd_excel_get(job_id, files=True)
        book = await asyncio.to_thread(load_book, job["source"])
        rows = await asyncio.to_thread(read_rows, book)
        inns = list(dict.fromkeys(row[-1] for row in rows if row[-1]))
        results = {}
        store.gtd_excel_progress(job_id, 0, len(inns))
        for start in range(0, len(inns), BATCH_SIZE):
            batch = inns[start:start + BATCH_SIZE]
            found = {}
            if mock:
                found = dict.fromkeys(batch, (MOCK, MOCK))
            else:
                for attempt in range(2):
                    try:
                        found = await asyncio.wait_for(lookup_batch(service.client, batch), timeout=45)
                        break
                    except (httpx.RequestError, ValueError, TimeoutError):
                        if attempt == 0:
                            await asyncio.sleep(1)
            results.update({inn: found.get(inn, (UNKNOWN, UNKNOWN)) for inn in batch})
            store.gtd_excel_progress(job_id, min(start + BATCH_SIZE, len(inns)), len(inns))
        output, report = await asyncio.to_thread(render_workbook, book, rows, results)
        store.gtd_excel_finish(job_id, output, report, {})
    except asyncio.CancelledError:
        store.gtd_excel_fail(job_id, "Обработка прервана. Загрузите файл повторно.")
        raise
    except Exception:
        store.gtd_excel_fail(job_id, "Не удалось обработать Excel. Проверьте файл и повторите загрузку.")
    finally:
        if book is not None:
            book.close()
