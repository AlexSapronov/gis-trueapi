"""Warehouse policies, independent of the existing general business_verdict."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from organizations import registry
from warehouse_excel import positive_integer
from warehouse_store import WarehouseError


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


async def shipment_results(service, doc):
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
    totals = {line['gtin']: 0 for line in doc['lines']}
    details, pns = [], {}
    for scan, r in zip(scans, results):
        reasons = []
        api = r.raw_api or {}
        if r.error_category or not r.api_checked:
            reasons.append(r.error_message or 'Проверка через True API не выполнена')
        if api.get('gtin') != scan['gtin']:
            reasons.append('GTIN в ответе True API не совпадает с отсканированным')
        if r.status != 'INTRODUCED':
            reasons.append(f'Недопустимый статус: {r.status or "не установлен"}. Нужен INTRODUCED (В обороте)')
        if not r.owner_inn:
            reasons.append('Владелец не установлен')
        elif doc['strict_owner'] and r.owner_inn != doc['org_inn']:
            reasons.append('КМ принадлежит другому юридическому лицу; замените КМ')
        elif not doc['strict_owner'] and not registry.has(r.owner_inn):
            reasons.append('КМ принадлежит сторонней организации; замените КМ')
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
                            status=r.status, owner_inn=r.owner_inn, owner_name=r.owner_name,
                            quantity=quantity, errors=reasons, api=r.as_dict(include_raw_api=True)))
    rows = [dict(gtin=l['gtin'], required=l['quantity'], actual=totals[l['gtin']],
                 remaining=l['quantity'] - totals[l['gtin']]) for l in doc['lines']]
    ok = bool(scans) and not any(d['errors'] for d in details) and all(r['remaining'] == 0 for r in rows)
    return dict(ok=ok, rows=rows, codes=details, missing=[], checked_count=len(scans)), pns


def receipt_results(doc):
    expected = {e['cis']: e for e in doc['expected']}
    totals = {l['gtin']: 0 for l in doc['lines']}
    accepted, details = set(), []
    for scan in doc['scans']:
        e, errors = expected.get(scan['cis']), []
        if e is None:
            errors.append('КМ отсутствует в документе приёмки')
        elif scan['full_code'] != e['full_code']:
            errors.append('Полный КМ не совпадает с ожидаемым: проверьте криптографическую часть')
        else:
            accepted.add(scan['cis'])
            totals[e['gtin']] += e['quantity']
        details.append(dict(scan_id=scan['id'], cis=scan['cis'], gtin=scan['gtin'],
                            quantity=e['quantity'] if e else None, errors=errors))
    missing = [dict(cis=e['cis'], gtin=e['gtin']) for cis, e in expected.items() if cis not in accepted]
    rows = [dict(gtin=l['gtin'], required=l['quantity'], actual=totals[l['gtin']],
                 remaining=l['quantity'] - totals[l['gtin']]) for l in doc['lines']]
    return dict(ok=bool(expected) and not missing and not any(d['errors'] for d in details),
                rows=rows, codes=details, missing=missing, checked_count=len(doc['scans']), accepted_count=len(accepted))


async def check_document(store, service, document_id):
    check_id, doc = store.begin_check(document_id)
    try:
        async with asyncio.timeout(540):
            if doc['kind'] == 'shipment':
                result, pns = await shipment_results(service, doc)
                missing = [l['gtin'] for l in doc['lines'] if not l['pn'] and l['gtin'] not in pns]
                resolved, warnings = await resolve_pns(service, missing, doc['org_inn']) if missing else ({}, [])
                store.fill_pns(document_id, {**pns, **resolved})
                result['warnings'] = warnings
            else:
                result = receipt_results(doc)
            result['checked_at'] = datetime.now(timezone.utc).isoformat()
            return store.finish_check(document_id, check_id, doc['version'], result)
    except BaseException:
        store.cancel_check(document_id, check_id)
        raise
