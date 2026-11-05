"""UPD 5.03 enrichment. XML/Decimal only; no network or database operations."""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
import re
import uuid

from lxml import etree as E

from warehouse_store import WarehouseError

MAX_XML_SIZE = 10 * 1024 * 1024
SCHEMA = Path(__file__).parent / 'schemas/ON_NSCHFDOPPR_1_997_01_05_03_05.xsd'


@lru_cache(maxsize=1)
def schema():
    return E.XMLSchema(E.parse(str(SCHEMA)))


def validate(root):
    try:
        schema().assertValid(root)
    except E.DocumentInvalid as exc:
        raise WarehouseError('XML не соответствует УПД 5.03: ' + str(exc.error_log.last_error)[:700]) from exc


def parse_xml(content):
    if not content or len(content) > MAX_XML_SIZE:
        raise WarehouseError('Размер XML должен быть от 1 байта до 10 МБ')
    try:
        root = E.fromstring(content, E.XMLParser(resolve_entities=False, load_dtd=False,
                            no_network=True, huge_tree=False, remove_blank_text=False))
    except (E.XMLSyntaxError, ValueError) as exc:
        raise WarehouseError('Не удалось прочитать XML УПД') from exc
    if root.getroottree().docinfo.doctype or root.xpath('//*[local-name()="Signature"]'):
        raise WarehouseError('Загрузите неподписанный XML без DTD и XML-подписей')
    if root.tag != 'Файл' or root.get('ВерсФорм') != '5.03':
        raise WarehouseError('Поддерживается только титул продавца УПД формата 5.03')
    validate(root)
    if root.find('Документ').get('Функция') not in {'СЧФДОП', 'ДОП'}:
        raise WarehouseError('Нужен УПД с функцией СЧФДОП или ДОП')
    return root


def pn_of(row):
    extra = row.find('ДопСведТов')
    article = extra.get('АртикулТов', '') if extra is not None else ''
    # 1C sample: "HQ25-31F2SURC62 (Светодиод)". Keep the full name visible.
    return article.strip() or re.sub(r'\s+\([^()]*\)\s*$', '', row.get('НаимТов', '')).strip()


def parties(root):
    def inn(tag):
        base = root.find('Документ/СвСчФакт/' + tag + '/ИдСв')
        if base is None:
            return ''
        for child in base:
            value = child.get('ИННЮЛ') or child.get('ИННФЛ')
            if value:
                return value
        return ''
    return inn('СвПрод'), inn('СвПокуп')


def check_document(doc):
    if (doc['kind'] != 'shipment' or doc['status'] != 'completed' or doc.get('deleted_at')
            or not doc.get('check_current') or not (doc.get('result') or {}).get('ok')):
        raise WarehouseError('XML доступен для завершённой отгрузки с актуальной успешной проверкой', 409)


def inspect_xml(content, doc, aliases):
    check_document(doc)
    root = parse_xml(content)
    seller, buyer = parties(root)
    if seller != doc['org_inn']:
        raise WarehouseError('ИНН продавца в XML не совпадает с организацией отгрузки')
    if doc['recipient_inn'] and buyer != doc['recipient_inn']:
        raise WarehouseError('ИНН покупателя в XML не совпадает с получателем отгрузки')
    if any(line['gtin'] and not line['pn'].strip() for line in doc['lines']):
        raise WarehouseError('Заполните PN всех маркируемых позиций отгрузки')
    choices = {}
    for line in doc['lines']:
        if line['gtin'] and line['pn'].strip():
            pn = line['pn'].strip()
            choice = choices.setdefault(pn, dict(pn=pn, gtins=[], quantity=0))
            choice['gtins'].append(line['gtin'])
            choice['quantity'] += line['quantity']
    if not choices:
        raise WarehouseError('Заполните PN маркируемых позиций отгрузки')
    rows = []
    for i, row in enumerate(root.findall('Документ/ТаблСчФакт/СведТов')):
        pn = pn_of(row)
        saved = aliases.get(pn)
        target = saved if saved in choices else pn if pn in choices else None
        rows.append(dict(id=str(i), number=row.get('НомСтр'), name=row.get('НаимТов', ''),
                         pn=pn, quantity=row.get('КолТов', ''), target_pn=target,
                         source='saved' if saved in choices else 'exact' if target else ''))
    return dict(rows=rows, choices=list(choices.values()), seller_inn=seller,
                number=root.find('Документ/СвСчФакт').get('НомерДок'),
                warnings=['PN взят из артикула или наименования. Проверьте распознавание и сопоставление.',
                          'Страна происхождения сохраняется из XML; запрос ГТД её не подтверждает.'])


def decimal(value, label):
    try:
        n = Decimal(value)
        if not n.is_finite() or n < 0:
            raise ValueError()
        return n
    except (InvalidOperation, ValueError, TypeError):
        raise WarehouseError('Некорректное число: ' + label)


def money_parts(value, quantities):
    total = decimal(value, 'сумма')
    if total != total.quantize(Decimal('.01')):
        raise WarehouseError('Сумма содержит более двух знаков после запятой')
    # Cumulative allocation avoids negative residuals for many small groups.
    result, previous, cumulative = [], Decimal(0), 0
    total_quantity = sum(quantities)
    for qty in quantities:
        cumulative += qty
        upto = (total * cumulative / total_quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        result.append(upto - previous)
        previous = upto
    return result


def allocate(root, doc, mapping):
    """Consume each scan once, preserving indivisible quantityInPack."""
    check_document(doc)
    rows = root.findall('Документ/ТаблСчФакт/СведТов')
    if set(mapping) != {str(i) for i in range(len(rows))}:
        raise WarehouseError('Сопоставьте каждую строку XML или явно выберите «Без маркировки»')
    by_gtin = {l['gtin']: l['pn'].strip() for l in doc['lines'] if l['gtin']}
    checked = {r['scan_id']: r for r in doc['result']['codes']}
    pools = defaultdict(deque)
    seen = set()
    for scan in doc['scans']:
        r = checked.get(scan['id'])
        if not r or r.get('errors') or type(r.get('quantity')) is not int or r['quantity'] <= 0:
            raise WarehouseError('Нет достоверного количества в КМ; повторите проверку отгрузки')
        # The warehouse identity is GTIN+serial, whereas True API/UPD require
        # explicit GS1 AIs: 01+GTIN+21+serial. Never send normalized_cis as API KI.
        gtin, identity = scan['gtin'], scan['cis']
        if identity.startswith('01' + gtin + '21'):
            cis = identity
        elif identity.startswith(gtin) and identity[len(gtin):]:
            cis = '01' + gtin + '21' + identity[len(gtin):]
        else:
            raise WarehouseError('Не удалось восстановить КИ из скана ' + scan['id'])
        if cis in seen:
            raise WarehouseError('Повторный КИ в отгрузке')
        seen.add(cis)
        pools[by_gtin[gtin]].append({**scan, 'cis': cis, 'quantity': r['quantity']})
    allocated = []
    for i, row in enumerate(rows):
        pn = mapping[str(i)]
        if pn is None:
            # Existing marking must never silently survive the "unmarked" path.
            if row.findall('.//НомСредИдентТов'):
                raise WarehouseError(f'Строка {i+1} уже содержит маркировку: выберите PN для замены')
            allocated.append([])
            continue
        if pn not in pools:
            raise WarehouseError(f'PN «{pn}» отсутствует в сканах')
        if row.get('ОКЕИ_Тов') != '796':
            raise WarehouseError(f'Строка {i+1}: для сопоставления нужна единица измерения «шт» (796)')
        qty = decimal(row.get('КолТов'), f'количество строки {i+1}')
        if not qty or qty != int(qty):
            raise WarehouseError(f'Строка {i+1}: количество должно быть положительным целым')
        selected, actual = [], 0
        while actual < qty and pools[pn]:
            scan = pools[pn].popleft()
            actual += scan['quantity']
            selected.append(scan)
        if actual != qty:
            raise WarehouseError(f'Строка {i+1}, PN {pn}: количество XML {qty} не совпадает с КМ ({actual}). '
                                 'КМ упаковки нельзя разделить между строками.')
        allocated.append(selected)
    leftovers = [pn for pn, pool in pools.items() if pool]
    if leftovers:
        raise WarehouseError('Не все КМ распределены по XML: ' + ', '.join(leftovers))
    return allocated


def split_amounts(original, clones, quantities):
    if len(clones) == 1:
        return
    extra = original.find('ДопСведТов')
    if (original.find('Акциз/СумАкциз') is not None or
            (extra is not None and (extra.findall('СведПрослеж') or extra.findall('НалУчАморт')
             or extra.findall('СумНалВосст') or extra.get('СтТовБезНДС') is not None))):
        raise WarehouseError('Автоматическое разбиение строк с акцизом, прослеживаемостью '
                             'или дополнительными налоговыми суммами не поддерживается')
    net = original.get('СтТовБезНДС')
    gross = original.get('СтТовУчНал')
    tax_node = original.find('СумНал/СумНал')
    tax = tax_node.text if tax_node is not None else '0'
    net_parts = money_parts(net, quantities) if net is not None else None
    tax_parts = money_parts(tax, quantities)
    if net is not None and gross is not None:
        if decimal(net, 'без НДС') + decimal(tax, 'НДС') != decimal(gross, 'с НДС'):
            raise WarehouseError('В исходной строке сумма без НДС + НДС не равна сумме с НДС')
        gross_parts = [n + t for n, t in zip(net_parts, tax_parts)]
    else:
        gross_parts = money_parts(gross, quantities) if gross is not None else None
    for i, clone in enumerate(clones):
        if net_parts is not None:
            clone.set('СтТовБезНДС', f'{net_parts[i]:.2f}')
        if gross_parts is not None:
            clone.set('СтТовУчНал', f'{gross_parts[i]:.2f}')
        if tax_node is not None:
            clone.find('СумНал/СумНал').text = f'{tax_parts[i]:.2f}'


def enrich_xml(content, doc, mapping, allocated, declarations=None):
    root = parse_xml(content)
    table = root.find('Документ/ТаблСчФакт')
    summary = []
    for i, original in enumerate(list(table.findall('СведТов'))):
        scans = allocated[i]
        if not scans:
            continue
        groups = defaultdict(list)
        for scan in scans:
            dt = declarations.get(scan['cis']) if declarations is not None else None
            if declarations is not None:
                if not isinstance(dt, str) or not dt.strip() or len(dt.strip()) > 29:
                    raise WarehouseError('ЧЗ не вернул корректный номер ГТД для КИ ' + scan['cis'])
                dt = dt.strip()
            groups[(dt, scan['gtin'])].append(scan)
        countries = {n.get('КодПроисх') for n in original.findall('СвДТ') if n.get('КодПроисх')}
        if declarations is not None and len(countries) > 1:
            raise WarehouseError(f'Строка {i+1}: несколько стран происхождения; уточните исходный XML')
        country = next(iter(countries), None)
        clones, quantities = [], []
        for (dt, gtin), codes in groups.items():
            clone = deepcopy(original)
            qty = sum(s['quantity'] for s in codes)
            clone.set('КолТов', str(qty))
            if declarations is not None:
                for old in clone.findall('СвДТ'):
                    clone.remove(old)
                declaration = E.Element('СвДТ', НомерДТ=dt)
                if country:
                    declaration.set('КодПроисх', country)
                clone.insert(0, declaration)
            extra = clone.find('ДопСведТов')
            if extra is None:
                extra = E.Element('ДопСведТов', ПрТовРаб='1')
                clone.insert(len(clone.findall('СвДТ')), extra)
            extra.set('ГТИН', gtin)
            for old in extra.findall('НомСредИдентТов'):
                extra.remove(old)
            marks = E.Element('НомСредИдентТов')
            for scan in codes:
                # UPD carries KI without crypto/GS/AI30, not the raw DataMatrix.
                E.SubElement(marks, 'КИЗ').text = scan['cis']
            state = extra.find('СвГосСист')
            extra.insert(extra.index(state) if state is not None else len(extra), marks)
            clones.append(clone)
            quantities.append(qty)
            summary.append(dict(source_row=i+1, pn=mapping[str(i)], gtin=gtin,
                                declaration_number=dt if declarations is not None else '; '.join(
                                    n.get('НомерДТ', '') for n in original.findall('СвДТ')),
                                quantity=qty, codes=len(codes)))
        split_amounts(original, clones, quantities)
        pos = table.index(original)
        table.remove(original)
        for offset, clone in enumerate(clones):
            table.insert(pos+offset, clone)
    for n, row in enumerate(table.findall('СведТов'), 1):
        row.set('НомСтр', str(n))
    # New file identity; N3 indicates that the document now contains marking.
    file_id = root.get('ИдФайл')
    parts = file_id.split('_')
    if len(parts) < 10 or not re.fullmatch(r'[0-9a-fA-F-]{36}', parts[-7]):
        raise WarehouseError('Не удалось разобрать ИдФайл УПД 5.03')
    parts[-7], parts[-5] = str(uuid.uuid4()), '1'
    root.set('ИдФайл', '_'.join(parts))
    validate(root)
    return E.tostring(root, encoding='windows-1251', xml_declaration=True), root.get('ИдФайл')+'.xml', summary
