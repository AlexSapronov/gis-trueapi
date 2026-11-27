"""Warehouse policies, independent of the existing general business_verdict."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from organizations import registry
from datamatrix import strip_ai30
from warehouse_excel import import_xlsx, positive_integer
import warehouse_store
from km_split_client import KmSplitError


async def resolve_pns(service, gtins, org_inn):
    """Only documented `model`; neither productName nor guesses are PN."""
    missing = set(gtins)
    found, warnings = {}, []
    inns = [org_inn] + [i for i in registry.inns() if i != org_inn]
    for inn in inns:
        if not missing:
            break
        token = service.tokens.get(inn)
        if not token:
            continue
        try:
            records = await service.client.product_info(sorted(missing), token)
        except Exception:
            warnings.append(f'Не удалось получить модель/артикул через True API для ИНН {inn}')
            continue
        for rec in records:
            gtin, model = rec.get('gtin'), rec.get('model')
            if gtin in missing and isinstance(model, str) and model.strip():
                found[gtin] = model.strip()
                missing.remove(gtin)
    if missing:
        warnings.append('Заполните PN вручную: ' + ', '.join(sorted(missing)))
    return found, warnings


async def import_document(store, service, content, kind, org_inn=None,
                          document_number=None, document_date=None, *, sales_username=None):
    header, lines, expected = await asyncio.to_thread(
        import_xlsx, content, kind, org_inn or None, document_number or None, document_date or None)
    warnings = []
    if kind == 'shipment' and store.application_settings()['values']['auto_pn']:
        missing = [line['gtin'] for line in lines if line['gtin'] and not line['pn']]
        try:
            async with asyncio.timeout(90):
                pns, warnings = await resolve_pns(service, missing, header['org_inn'])
        except TimeoutError:
            pns, warnings = {}, ['Получение PN заняло слишком много времени. Заполните PN вручную.']
        for line in lines:
            if not line['pn'] and line['gtin'] in pns:
                line.update(pn=pns[line['gtin']], pn_source='trueapi')
    document = await asyncio.to_thread(store.create, header, lines, expected, sales_username=sales_username)
    return {**document, 'import_warnings': warnings}


def reconcile_gtd_fifo(doc, details):
    if not doc.get('gtd_required'):
        return None
    state = doc.get('gtd_state') or 'pending'
    if state != 'ready' or not doc.get('gtd_rows'):
        return dict(ok=False, state=state,
                    message=doc.get('gtd_message') or 'Ожидайте загрузки данных по ГТД. Груз пока не упаковывать.',
                    split_required=[], missing=[], extra=[])
    rows_by_gtin = {}
    for row in doc['gtd_rows']:
        rows_by_gtin.setdefault(row['gtin'], []).append(row)
    scans_by_gtin = {}
    split_order = {}
    for split in doc.get('splits', []):
        for index, item in enumerate(split.get('expected_codes') or []):
            split_order[item['cis']] = (split.get('source_scanned_at') or split.get('created_at') or '',
                                        split.get('created_at') or '', index)
    for detail in details:
        scans_by_gtin.setdefault(detail['gtin'], []).append(detail)
    for values in scans_by_gtin.values():
        values.sort(key=lambda item: split_order.get(
            item['cis'], (item.get('scanned_at') or '', '', -1)))
    split_required, missing, extra = [], [], []
    has_invalid_scan = any(detail.get('errors') for detail in details)
    for gtin, target_rows in rows_by_gtin.items():
        target_rows = sorted(target_rows, key=lambda row: row.get('position', row.get('source_row', 0)))
        row_index, row_remaining = 0, target_rows[0]['quantity']
        for scan in scans_by_gtin.get(gtin, []):
            quantity = scan.get('quantity')
            if scan.get('errors') or type(quantity) is not int or quantity <= 0:
                has_invalid_scan = True
                continue
            left = quantity
            chunks, declarations = [], []
            while left > 0 and row_index < len(target_rows):
                take = min(left, row_remaining)
                chunks.append(take)
                declarations.append(target_rows[row_index]['declaration'])
                left -= take
                row_remaining -= take
                if row_remaining == 0:
                    row_index += 1
                    if row_index < len(target_rows):
                        row_remaining = target_rows[row_index]['quantity']
            if left > 0:
                extra.append(dict(gtin=gtin, scan_id=scan['scan_id'], quantity=left))
            if left == 0 and len(chunks) > 1:
                split_required.append(dict(
                    scan_id=scan['scan_id'], cis=scan['cis'], gtin=gtin,
                    original_quantity=quantity, parts=chunks[:-1],
                    remaining_quantity=chunks[-1], declarations=declarations))
        if row_index < len(target_rows):
            remaining = row_remaining + sum(row['quantity'] for row in target_rows[row_index + 1:])
            missing.append(dict(gtin=gtin, quantity=remaining))
    for gtin in scans_by_gtin:
        if gtin not in rows_by_gtin:
            extra.append(dict(gtin=gtin, quantity=sum(
                detail.get('quantity') or 0 for detail in scans_by_gtin[gtin])))
    ok = not split_required and not missing and not extra and not has_invalid_scan
    if split_required:
        message = 'Необходимо разделить КМ для соответствия строкам ГТД. Упаковку пока не начинать.'
    elif missing or extra:
        message = 'Количество по строкам ГТД не совпадает со сканами. Проверьте состав отгрузки.'
    elif has_invalid_scan:
        message = 'Есть КМ с ошибками проверки. Исправьте их перед упаковкой.'
    else:
        message = 'ГТД распределены по физическим КМ. Можно паковать.'
    return dict(ok=ok, state='ready' if ok else 'mismatch', message=message,
                split_required=split_required, missing=missing, extra=extra)


async def shipment_results(service, doc):
    policy = doc.get('runtime_settings', {}).get('values', {})
    allowed = policy.get('shipment_statuses', ['INTRODUCED'])
    scans = doc['scans']
    results = await service.scan_batch([s['raw_code'] for s in scans], doc['org_inn'])
    # Owner fields may be visible only to another organization. Retry only unresolved codes.
    for inn in registry.inns():
        if inn == doc['org_inn'] or not service.tokens.has(inn):
            continue
        unresolved = [i for i, r in enumerate(results) if r.error_category or not r.owner_inn]
        if not unresolved:
            break
        alternatives = await service.scan_batch([scans[i]['raw_code'] for i in unresolved], inn)
        for i, alt in zip(unresolved, alternatives):
            if not alt.error_category and alt.owner_inn:
                results[i] = alt
    marked = [line for line in doc['lines'] if line['gtin']]
    totals = {line['gtin']: 0 for line in marked}
    details, pns = [], {}
    warn_on_owner_mismatch = warehouse_store.shipment_owner_warning_active()
    for scan, r in zip(scans, results):
        reasons = []
        owner_mismatch = None
        api = r.raw_api or {}
        if r.error_category or not r.api_checked:
            reasons.append(r.error_message or 'Проверка через True API не выполнена')
        if api.get('gtin') != scan['gtin']:
            reasons.append('GTIN в ответе True API не совпадает с отсканированным')
        if r.status not in allowed:
            reasons.append(f'Недопустимый статус: {r.status or "не установлен"}. Допустимы: {", ".join(allowed)}')
        if not r.owner_inn:
            reasons.append('Владелец не установлен')
        elif doc['strict_owner'] and r.owner_inn != doc['org_inn']:
            owner_mismatch = 'КМ принадлежит другому юридическому лицу'
        elif not doc['strict_owner'] and not registry.has(r.owner_inn):
            owner_mismatch = 'КМ принадлежит сторонней организации'
        if owner_mismatch and not warn_on_owner_mismatch:
            reasons.append(owner_mismatch + '; замените КМ')
        quantity = None
        try:
            quantity = positive_integer(r.quantity_in_pack)
        except ValueError:
            reasons.append('Нет достоверного положительного целого quantityInPack')
        if scan['gtin'] not in totals:
            reasons.append('Лишний GTIN, отсутствует в документе')
        if not scan['has_crypto']:
            reasons.append('Нет полной криптографической части; замените скан полным КМ')
        if not reasons:
            totals[scan['gtin']] += quantity
        if isinstance(api.get('model'), str) and api['model'].strip():
            pns[scan['gtin']] = api['model'].strip()
        details.append(dict(scan_id=scan['id'], cis=scan['cis'], gtin=scan['gtin'],
                            scanned_at=scan['scanned_at'], status=r.status, owner_inn=r.owner_inn,
                            owner_name=r.owner_name, quantity=quantity, errors=reasons,
                            api=r.as_dict(include_raw_api=True)))
        if owner_mismatch and warn_on_owner_mismatch:
            details[-1]['owner_warning'] = owner_mismatch
    rows = [dict(gtin=l['gtin'], required=l['quantity'], actual=totals[l['gtin']],
                 remaining=l['quantity'] - totals[l['gtin']]) for l in marked]
    gtd = reconcile_gtd_fifo(doc, details)
    ok = ((bool(scans) or not marked) and not any(d['errors'] for d in details)
          and all(r['remaining'] == 0 for r in rows) and (gtd is None or gtd['ok']))
    return dict(ok=ok, rows=rows, codes=details, missing=[], checked_count=len(scans),
                owner_mismatch_warnings=sum('owner_warning' in d for d in details), gtd=gtd), pns


def _receipt_full_code_error(expected_code: str, scanned_code: str, expected_quantity: int) -> str | None:
    """Compare the *entire* KM and its crypto, ignoring GS/FNC1 and AI 30.

    All recognized GS/FNC1 representations are treated identically, including
    partially lost separators. The optional AI 30 is packaging metadata, not
    part of KM identity, and must never determine receipt acceptance.
    """
    expected = strip_ai30(expected_code).replace('\x1d', '')
    scanned = strip_ai30(scanned_code).replace('\x1d', '')
    if scanned == expected:
        return None
    # If GS was dropped before AI 30 in an otherwise equal KM, allow that
    # trailing AI too. A suffix is accepted only after an *exact* crypto match.
    for short, long in ((expected, scanned), (scanned, expected)):
        if long.startswith(short) and re.fullmatch(r'30[0-9]{1,8}', long[len(short):]):
            return None
    return 'Полный КМ не совпадает с ожидаемым: проверьте криптографическую часть'

def receipt_results(doc):
    expected = {e['cis']: e for e in doc['expected']}
    marked = [line for line in doc['lines'] if line['gtin']]
    totals = {l['gtin']: 0 for l in marked}
    accepted, details = set(), []
    for scan in doc['scans']:
        e, errors = expected.get(scan['cis']), []
        if e is None:
            errors.append('КМ отсутствует в документе приёмки')
        else:
            mismatch = _receipt_full_code_error(
                e['full_code'], scan['full_code'], e['quantity']
            )
            if mismatch:
                errors.append(mismatch)
            else:
                accepted.add(scan['cis'])
                totals[e['gtin']] += e['quantity']
        details.append(dict(scan_id=scan['id'], cis=scan['cis'], gtin=scan['gtin'],
                            quantity=e['quantity'] if e else None, errors=errors))
    missing = [dict(cis=e['cis'], gtin=e['gtin']) for cis, e in expected.items() if cis not in accepted]
    rows = [dict(gtin=l['gtin'], required=l['quantity'], actual=totals[l['gtin']],
                 remaining=l['quantity'] - totals[l['gtin']]) for l in marked]
    return dict(ok=(bool(expected) or not marked) and not missing and not any(d['errors'] for d in details),
                rows=rows, codes=details, missing=missing, checked_count=len(doc['scans']), accepted_count=len(accepted))


async def refresh_gtd_splits(store, service, doc):
    client = getattr(service, 'km_split_client', None)
    if not client or not client.enabled:
        return doc
    for split in doc.get('splits', []):
        if split['state'] in ('done', 'failed') or not split.get('external_batch_id'):
            continue
        try:
            remote = await client.status(split['external_batch_id'])
            store.update_split(doc['id'], split['id'], remote)
            doc = store.get(doc['id'])
            if remote.get('state') == 'ready_to_rescan':
                expected = remote.get('expected_codes') or []
                expected_cis = {item['cis'] for item in expected}
                scanned = {item['cis']: item for item in doc['scans'] if item['cis'] in expected_cis}
                if (expected_cis and set(scanned) == expected_cis
                        and all(scanned[cis]['has_crypto'] for cis in expected_cis)):
                    try:
                        remote = await client.confirm(split['external_batch_id'], list(expected_cis))
                        store.update_split(doc['id'], split['id'], remote)
                        doc = store.get(doc['id'])
                    except KmSplitError as exc:
                        # The POST may have reached km-split. Poll its idempotent batch status
                        # before attempting confirmation again on the next check.
                        store.update_split(doc['id'], split['id'],
                                           {'state':'ready_to_rescan',
                                            'expected_codes':expected,
                                            'error':'Не удалось получить подтверждение отправки DIVISIONMARK: ' + str(exc)})
                        doc = store.get(doc['id'])
        except KmSplitError as exc:
            store.update_split(doc['id'], split['id'],
                               {'state':split['state'], 'expected_codes':split.get('expected_codes') or [],
                                'error':'Не удалось обновить состояние разделения: ' + str(exc)})
            doc = store.get(doc['id'])
    return doc


async def start_gtd_splits(store, service, doc, result):
    client = getattr(service, 'km_split_client', None)
    plans = (result.get('gtd') or {}).get('split_required') or []
    if not plans:
        return result, doc
    if not client or not client.enabled:
        result['ok'] = False
        result['gtd']['state'] = 'blocked'
        result['gtd']['message'] = ('Найдены КМ, которые нужно разделить для соответствия ГТД, '
                                    'но автоматическое разделение не настроено. Упаковка запрещена.')
        return result, doc
    scans = {scan['id']: scan for scan in doc['scans']}
    started = []
    for plan in plans:
        scan = scans.get(plan['scan_id'])
        if not scan:
            continue
        request_id = str(uuid5(NAMESPACE_URL,
                               f"gis-trueapi:{doc['id']}:{scan['id']}:{','.join(map(str, plan['parts']))}"))
        try:
            remote = await client.create(scan['raw_code'], plan['parts'], request_id)
            local = store.create_split(doc['id'], scan['id'], request_id, plan['parts'], remote)
            started.extend(item['id'] for item in local.get('splits', [])
                           if item.get('request_id') == request_id)
        except KmSplitError as exc:
            result['ok'] = False
            result['gtd']['state'] = 'blocked'
            result['gtd']['message'] = ('Не удалось запустить автоматическое разделение КМ. '
                                        'Исходный скан сохранён; повторная отправка использует тот же идентификатор. '
                                        + str(exc))
            return result, doc
    result['ok'] = False
    result['gtd']['state'] = 'splitting'
    result['gtd']['message'] = ('Программа запустила автоматическое разделение КМ. '
                                'Ожидайте выпуска новых кодов. Упаковку пока не начинать.')
    result['gtd']['split_started'] = True
    return result, store.get(doc['id'])


async def check_document(store, service, document_id):
    check_id, doc = store.begin_check(document_id)
    check_version = doc['version']
    policy = doc['runtime_settings']['values']
    try:
        async with asyncio.timeout(policy['check_timeout_seconds']):
            if doc['kind'] == 'shipment':
                doc = await refresh_gtd_splits(store, service, doc)
                result, pns = await shipment_results(service, doc)
                if (result.get('gtd') or {}).get('split_required'):
                    result, doc = await start_gtd_splits(store, service, doc, result)
                missing = [l['gtin'] for l in doc['lines'] if l['gtin'] and not l['pn'] and l['gtin'] not in pns]
                resolved, warnings = await resolve_pns(service, missing, doc['org_inn']) if missing and policy['auto_pn'] else ({}, [])
                if policy['auto_pn']:
                    store.fill_pns(document_id, {**pns, **resolved})
                result['warnings'] = warnings
            else:
                result = receipt_results(doc)
            result['checked_at'] = datetime.now(timezone.utc).isoformat()
            result['policy'] = {'settings_revision': doc['runtime_settings']['revision'],
                                'shipment_statuses': policy['shipment_statuses'], 'strict_owner': bool(doc['strict_owner'])}
            return store.finish_check(document_id, check_id, check_version, result)
    except BaseException:
        store.cancel_check(document_id, check_id)
        raise
