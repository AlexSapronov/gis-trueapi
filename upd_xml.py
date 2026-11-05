"""UPD 5.03 enrichment. XML/Decimal only; no network or database operations."""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict, deque
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
                         pn=pn, quantity=row.get('КолТов', ''),
                         declarations=[n.get('НомерДТ', '') for n in row.findall('СвДТ')], target_pn=target,
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


class Allocation(list):
    """Per-original-row scans plus the source rows rebuilt as one commercial unit."""
    def __init__(self, size):
        super().__init__([[] for _ in range(size)])
        self.groups = {}


def commercial_signature(row):
    """Only customs, marking, quantity and amounts may differ when merging."""
    clone = deepcopy(row)
    for name in ('НомСтр', 'КолТов', 'СтТовБезНДС', 'СтТовУчНал'):
        if name in clone.attrib:
            if name in ('СтТовБезНДС', 'СтТовУчНал'):
                clone.set(name, '')
            else:
                clone.attrib.pop(name)
    for node in clone.findall('СвДТ') + clone.findall('.//НомСредИдентТов'):
        node.getparent().remove(node)
    extra = clone.find('ДопСведТов')
    if extra is not None:
        extra.attrib.pop('ГТИН', None)
    tax = clone.find('СумНал/СумНал')
    if tax is not None:
        tax.text = ''
    for node in clone.iter():
        node.tail = None
        if node.text and not node.text.strip():
            node.text = None
    return E.tostring(clone, method='c14n')


def allocate(root, doc, mapping, regroup=False):
    """Reconcile PN totals; in customs mode ignore old 1C customs boundaries."""
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
    units = {}
    for i, row in enumerate(rows):
        pn = mapping[str(i)]
        if pn is None:
            if row.findall('.//НомСредИдентТов'):
                raise WarehouseError(f'Строка {i+1} уже содержит маркировку: выберите PN для замены')
            continue
        if pn not in pools:
            raise WarehouseError(f'PN «{pn}» отсутствует в сканах')
        if row.get('ОКЕИ_Тов') != '796':
            raise WarehouseError(f'Строка {i+1}: для сопоставления нужна единица измерения «шт» (796)')
        qty = decimal(row.get('КолТов'), f'количество строки {i+1}')
        if not qty or qty != int(qty):
            raise WarehouseError(f'Строка {i+1}: количество должно быть положительным целым')
        key = (pn, commercial_signature(row)) if regroup else i
        unit = units.setdefault(key, dict(pn=pn, indices=[], quantity=0))
        unit['indices'].append(i)
        unit['quantity'] += int(qty)
    # Report actual discrepancies separately from an artificial boundary inside one PN.
    totals = Counter()
    for unit in units.values():
        totals[unit['pn']] += unit['quantity']
    for pn, pool in pools.items():
        actual = sum(scan['quantity'] for scan in pool)
        if totals[pn] != actual:
            raise WarehouseError(f'PN {pn}: общее количество XML {totals[pn]} не совпадает с КМ ({actual}). '
                                 'Не все КМ распределены по XML или не хватает товара.')
    allocated = Allocation(len(rows))
    for unit in units.values():
        pn, qty, indices = unit['pn'], unit['quantity'], unit['indices']
        selected, actual = [], 0
        while actual < qty and pools[pn]:
            scan = pools[pn].popleft()
            actual += scan['quantity']
            selected.append(scan)
        if actual != qty:
            label = ', '.join(str(i+1) for i in indices)
            raise WarehouseError(f'Строки {label}, PN {pn}: граница количества XML {qty} '
                                 f'проходит внутри КМ ({actual}). Нельзя делить упаковку. '
                                 'Строки с разной ценой, НДС или другими реквизитами автоматически не объединяются.',
                                 code='upd_pack_boundary')
        allocated[indices[0]] = selected
        allocated.groups[indices[0]] = indices
    return allocated


def package_counts(scans):
    return [dict(quantity=qty, codes=count, total=qty*count)
            for qty, count in sorted(Counter(s['quantity'] for s in scans).items())]


def original_rows(rows, indices):
    return [dict(row=i+1, quantity=rows[i].get('КолТов'),
                 declarations=[n.get('НомерДТ', '') for n in rows[i].findall('СвДТ')]) for i in indices]


def review_plan(root, mapping, allocated, declarations=None):
    """Explain exact whole-KI quantities without assigning unverifiable customs data."""
    rows = root.findall('Документ/ТаблСчФакт/СведТов')
    proposed, boundaries = [], []
    for index, scans in enumerate(allocated):
        if not scans:
            continue
        indices = allocated.groups.get(index, [index])
        source = original_rows(rows, indices)
        groups = defaultdict(list)
        for scan in scans:
            groups[(declarations.get(scan['cis']) if declarations is not None else None,
                    scan['gtin'])].append(scan)
        for (dt, gtin), codes in groups.items():
            proposed.append(dict(source_row=index+1, source_rows=[i+1 for i in indices],
                                 original_rows=source, pn=mapping[str(index)], gtin=gtin,
                                 declaration_number=dt, quantity=sum(s['quantity'] for s in codes),
                                 codes=len(codes), packages=package_counts(codes)))
        # An explicit alternative retaining as many old rows as possible. Only
        # quantities are suggested; this does not establish which DT owns a KI.
        prefix = [0]
        for scan in scans:
            prefix.append(prefix[-1]+scan['quantity'])
        count = min(len(indices), len(scans))
        previous, target = 0, Decimal(0)
        for n in range(count):
            target += decimal(rows[indices[n]].get('КолТов'), 'количество')
            if n == count-1:
                stop = len(scans)
            else:
                low, high = previous+1, len(scans)-(count-n-1)
                # Binary search on cumulative pack quantities; do not scan all
                # candidate boundaries for each XML row.
                candidate = min(high, max(low, bisect_left(prefix, target)))
                stop = min({candidate, max(low, candidate-1)}, key=lambda k: (abs(prefix[k]-target), k))
            selected = scans[previous:stop]
            boundaries.append(dict(source_rows=[i+1 for i in indices], pn=mapping[str(index)],
                                   original_row=indices[n]+1, quantity=prefix[stop]-prefix[previous],
                                   codes=len(selected), packages=package_counts(selected),
                                   requires_customs_confirmation=True))
            previous = stop
    checked = declarations is not None
    message = (
        'ЧЗ вернул сведения о части КМ без номера ГТД. Это не означает отсутствия декларации. '
        if checked else
        'Границы исходных строк проходят внутри КМ. Для пересборки по ГТД включите получение ГТД из ЧЗ '
        'или исправьте количество в строках по приведённому варианту. '
    )
    message += ('Ниже приведены количества по целым КМ. Вариант границ исходных строк '
                'не подтверждает соответствие ГТД конкретным КМ. ')
    if checked:
        message += ('Подтвердите исходные ГТД и исправьте строки в 1С, затем загрузите XML повторно '
                    'с выключенным получением ГТД, чтобы сохранить подтверждённые декларации.')
    return dict(ready=False, needs_review=True, gtd_checked=checked,
                rows=proposed, boundaries=boundaries, message=message)



def merge_rows(rows):
    merged = deepcopy(rows[0])
    if len(rows) == 1:
        return merged
    for row in rows:
        validate_rebuild_amounts(row)
    merged.set('КолТов', str(sum(decimal(row.get('КолТов'), 'количество') for row in rows)))
    for attr in ('СтТовБезНДС', 'СтТовУчНал'):
        if rows[0].get(attr) is not None:
            merged.set(attr, format(sum(decimal(row.get(attr), attr) for row in rows), '.2f'))
    tax = merged.find('СумНал/СумНал')
    if tax is not None:
        tax.text = format(sum(decimal(row.findtext('СумНал/СумНал'), 'НДС') for row in rows), '.2f')
    return merged


def validate_rebuild_amounts(original):
    extra = original.find('ДопСведТов')
    if (original.find('Акциз/СумАкциз') is not None or
            (extra is not None and (extra.findall('СведПрослеж') or extra.findall('НалУчАморт')
             or extra.findall('СумНалВосст') or extra.get('СтТовБезНДС') is not None))):
        raise WarehouseError('Автоматическое разбиение строк с акцизом, прослеживаемостью '
                             'или дополнительными налоговыми суммами не поддерживается')
    net, gross = original.get('СтТовБезНДС'), original.get('СтТовУчНал')
    tax = original.findtext('СумНал/СумНал', '0')
    for value in (net, gross, tax):
        if value is not None:
            amount = decimal(value, 'сумма')
            if amount != amount.quantize(Decimal('.01')):
                raise WarehouseError('Сумма содержит более двух знаков после запятой')
    if net is not None and gross is not None:
        if decimal(net, 'без НДС') + decimal(tax, 'НДС') != decimal(gross, 'с НДС'):
            raise WarehouseError('В исходной строке сумма без НДС + НДС не равна сумме с НДС')


def split_amounts(original, clones, quantities):
    if len(clones) == 1:
        return
    validate_rebuild_amounts(original)
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
    source_rows = list(table.findall('СведТов'))
    for i, source_row in enumerate(source_rows):
        scans = allocated[i]
        if not scans:
            continue
        indices = getattr(allocated, 'groups', {}).get(i, [i])
        originals = [source_rows[index] for index in indices]
        original = merge_rows(originals)
        groups = defaultdict(list)
        for scan in scans:
            dt = declarations.get(scan['cis']) if declarations is not None else None
            if declarations is not None:
                if not isinstance(dt, str) or not dt.strip() or len(dt.strip()) > 29:
                    raise WarehouseError('ЧЗ не вернул корректный номер ГТД для КИ ' + scan['cis'])
                dt = dt.strip()
            groups[(dt, scan['gtin'])].append(scan)
        countries = {n.get('КодПроисх') for row in originals for n in row.findall('СвДТ') if n.get('КодПроисх')}
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
            summary.append(dict(source_row=i+1, source_rows=[index+1 for index in indices],
                                original_rows=original_rows(source_rows, indices), pn=mapping[str(i)], gtin=gtin,
                                declaration_number=dt if declarations is not None else '; '.join(
                                    n.get('НомерДТ', '') for n in original.findall('СвДТ')),
                                quantity=qty, codes=len(codes)))
        split_amounts(original, clones, quantities)
        pos = table.index(source_row)
        for old in originals:
            table.remove(old)
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
