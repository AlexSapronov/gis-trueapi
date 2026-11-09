"""Vector GS1 DataMatrix labels for the unscanned part of a checked receipt."""
from io import BytesIO
from pathlib import Path

from reportlab.graphics.barcode.ecc200datamatrix import ECC200DataMatrix
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from datamatrix import radio_label_payload
from warehouse_store import WarehouseError


MAX_LABELS = 1000
FONT_PATH = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
FONT = 'Helvetica'
if FONT_PATH.is_file():
    FONT = 'ReceiptLabelSans'
    pdfmetrics.registerFont(TTFont(FONT, str(FONT_PATH)))


class GS1Matrix(ECC200DataMatrix):
    """ECC200 ASCII encodation ported from marking-bm/marking_bm/labels.py."""

    def _encode_c40(self, value):
        data, pos = [232], 0
        while pos < len(value):
            pair = value[pos:pos + 2]
            if len(pair) == 2 and pair.isdigit():
                data.append(130 + int(pair))
                pos += 2
            else:
                # ASCII GS (29) is codeword 30. FNC1 (232) is the prefix only.
                data.append(ord(value[pos]) + 1)
                pos += 1
        if len(data) > self.cw_data:
            raise ValueError('КМ слишком длинный для этикетки')
        if len(data) < self.cw_data:
            data.append(129)
        while len(data) < self.cw_data:
            pad = 129 + ((149 * (len(data) + 1)) % 253) + 1
            data.append(pad if pad <= 254 else pad - 254)
        return data


def pn_lines(pn):
    if not pn:
        raise ValueError('Заполните PN в документе и сохраните изменения')
    if FONT == 'Helvetica' and not pn.isascii():
        raise ValueError('Для печати PN с нелатинскими символами установите шрифт DejaVu Sans')
    lines, line = [], ''
    for char in pn:
        if pdfmetrics.stringWidth(line + char, FONT, 9) > 26 * mm:
            lines.append(line)
            line = ''
        line += char
    lines.append(line)
    if len(lines) > 4:
        raise ValueError('PN не помещается на этикетке 58×40 мм')
    return lines


def draw_label(canvas, full_code, pn, quantity):
    lines = pn_lines(pn)
    barcode = GS1Matrix(full_code)
    barcode.validate()
    matrix = barcode.encode()
    # Use four whole dots per module at the common 203 dpi printer resolution.
    module = 4 * 72 / 203
    path = canvas.beginPath()
    for row, pixels in enumerate(matrix):
        for col, pixel in enumerate(pixels):
            if pixel:
                path.rect(3 * mm + col * module, 9 * mm + row * module, module, module)
    canvas.drawPath(path, stroke=0, fill=1)
    canvas.setFont(FONT, 9)
    for index, line in enumerate(lines):
        canvas.drawCentredString(42 * mm, (29 - 4 * index) * mm, line)
    canvas.setFont(FONT, 12)
    units = 'pcs' if FONT == 'Helvetica' else 'шт.'
    canvas.drawCentredString(42 * mm, 9 * mm, f'{quantity} {units}')
    canvas.showPage()


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
    output = BytesIO()
    canvas = Canvas(output, pagesize=(58 * mm, 40 * mm), pageCompression=1)
    for code in doc['expected']:
        if code['cis'] not in missing:
            continue
        try:
            full = radio_label_payload(code['full_code'])
            draw_label(canvas, full, pns[code['gtin']].strip(), code['quantity'])
        except ValueError as exc:
            raise WarehouseError(f"КИ {code['cis']}: {exc}") from exc
    canvas.save()
    return output.getvalue()
