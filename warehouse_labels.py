"""Assemble a PDF from the missing labels of a checked receipt."""
from io import BytesIO

from pypdf import PdfReader, PdfWriter

from datamatrix import radio_label_payload
from marking_bm_pdf.labels import render_label
from warehouse_store import WarehouseError


MAX_LABELS = 1000


def missing_receipt_pdf(doc):
    if doc['kind'] != 'receipt' or doc['status'] != 'active' or doc['archived_at']:
        raise WarehouseError('Этикетки доступны для активной приёмки', 409)
    if not doc['check_current'] or doc['checking']:
        raise WarehouseError('Сначала выполните актуальную проверку приёмки на складе', 409)
    scanned = {scan['cis'] for scan in doc['scans']}
    missing = {code['cis'] for code in doc['result']['missing']} - scanned
    if not missing:
        raise WarehouseError('Нет неотсканированных КМ для печати', 409)
    if len(missing) > MAX_LABELS:
        raise WarehouseError(f'Слишком много недостающих КМ. За раз можно сформировать до {MAX_LABELS} этикеток.')

    pns = {line['gtin']: line['pn'] for line in doc['lines']}
    writer = PdfWriter()
    for code in doc['expected']:
        if code['cis'] not in missing:
            continue
        try:
            full = radio_label_payload(code['full_code'])
            pn = pns[code['gtin']].strip()
            if not pn:
                raise ValueError('Заполните PN в документе и сохраните изменения')
            page = render_label(full, pn, code['quantity'])
            writer.add_page(PdfReader(BytesIO(page)).pages[0])
        except ValueError as exc:
            raise WarehouseError(f"КИ {code['cis']}: {exc}") from exc
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
