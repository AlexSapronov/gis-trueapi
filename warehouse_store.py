"""Durable document state. Short SQLite transactions; no network inside locks."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from warehouse_issues import WarehouseIssues
from upd_store import UpdStore
from gtd_excel_store import GtdExcelStore
from app_settings import RuntimeSettings
from receipt_bridge import ReceiptBridgeStore


class WarehouseError(Exception):
    def __init__(self, message, status=422, code=None):
        self.message, self.status = message, status
        self.code = code
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def shipment_owner_warning_active():
    # 10 December 2026, 00:00 Moscow time.
    return datetime.now(timezone.utc) < datetime(2026, 12, 9, 21, tzinfo=timezone.utc)


def _check_current(doc):
    result = doc['result']
    return (result is not None and doc['result_version'] == doc['version']
            and (doc['status'] == 'completed' or not result.get('owner_mismatch_warnings')
                 or shipment_owner_warning_active()))


class WarehouseStore(ReceiptBridgeStore, WarehouseIssues, RuntimeSettings, UpdStore, GtdExcelStore):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=15) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
            CREATE TABLE IF NOT EXISTS warehouse_documents (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, number TEXT NOT NULL,
              date TEXT NOT NULL, org_inn TEXT NOT NULL, org_name TEXT NOT NULL,
              recipient_name TEXT NOT NULL, recipient_inn TEXT NOT NULL,
              strict_owner INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
              version INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
              check_id TEXT, check_until REAL, result TEXT,
              result_version INTEGER, result_at TEXT, deleted_at TEXT, archived_at TEXT,
              gtd_required INTEGER NOT NULL DEFAULT 0, gtd_state TEXT NOT NULL DEFAULT 'none',
              gtd_message TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS warehouse_shipment_sources (
              org_inn TEXT NOT NULL, invoice_id TEXT NOT NULL,
              document_id TEXT NOT NULL UNIQUE REFERENCES warehouse_documents(id),
              PRIMARY KEY(org_inn, invoice_id));
            CREATE TABLE IF NOT EXISTS warehouse_shipment_items (
              document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              good_id TEXT NOT NULL, gtin TEXT NOT NULL, pn TEXT NOT NULL, quantity INTEGER NOT NULL,
              PRIMARY KEY(document_id,good_id,gtin));
            CREATE TABLE IF NOT EXISTS warehouse_gtd_rows (
              document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              position INTEGER NOT NULL, good_id TEXT NOT NULL, gtin TEXT NOT NULL,
              quantity INTEGER NOT NULL, declaration TEXT NOT NULL,
              source_sheet TEXT NOT NULL, source_row INTEGER NOT NULL,
              PRIMARY KEY(document_id,position));
            CREATE TABLE IF NOT EXISTS warehouse_splits (
              id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              source_scan_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
              source_cis TEXT NOT NULL, parts TEXT NOT NULL, external_batch_id TEXT,
              expected_codes TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL,
              error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS warehouse_splits_doc ON warehouse_splits(document_id,state);
            CREATE TABLE IF NOT EXISTS gis_signer_jobs (
              id TEXT PRIMARY KEY, inn TEXT NOT NULL, challenge_uuid TEXT NOT NULL,
              challenge_data TEXT NOT NULL, sha256 TEXT NOT NULL, state TEXT NOT NULL,
              lease TEXT, lease_until REAL, error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS gis_signer_jobs_queue ON gis_signer_jobs(state,created_at);
            CREATE TABLE IF NOT EXISTS gis_signer_heartbeat (
              id INTEGER PRIMARY KEY CHECK(id=1), thumbprint TEXT NOT NULL,
              expires TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS warehouse_lines (
              document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              gtin TEXT NOT NULL, pn TEXT NOT NULL, pn_source TEXT NOT NULL,
              quantity INTEGER NOT NULL CHECK(quantity > 0),
              PRIMARY KEY(document_id, gtin));
            CREATE TABLE IF NOT EXISTS warehouse_expected (
              document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              cis TEXT NOT NULL, raw_code TEXT NOT NULL, full_code TEXT NOT NULL,
              gtin TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
              PRIMARY KEY(document_id, cis));
            CREATE TABLE IF NOT EXISTS warehouse_unmarked_lines (
              id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              pn TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0));
            CREATE INDEX IF NOT EXISTS warehouse_unmarked_doc ON warehouse_unmarked_lines(document_id);
            CREATE TABLE IF NOT EXISTS warehouse_scans (
              id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              cis TEXT NOT NULL, raw_code TEXT NOT NULL, full_code TEXT NOT NULL,
              gtin TEXT NOT NULL, has_crypto INTEGER NOT NULL, scanned_at TEXT NOT NULL,
              UNIQUE(document_id, cis));
            CREATE TABLE IF NOT EXISTS warehouse_checks (
              id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              version INTEGER NOT NULL, created_at TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS warehouse_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              created_at TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,details TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS warehouse_events_doc ON warehouse_events(document_id,id);
            CREATE TABLE IF NOT EXISTS warehouse_issues (
              id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES warehouse_documents(id),version INTEGER NOT NULL,
              result_at TEXT NOT NULL,reported_at TEXT NOT NULL,message TEXT NOT NULL,snapshot TEXT NOT NULL,
              resolved_at TEXT,resolution TEXT);
            CREATE INDEX IF NOT EXISTS warehouse_issues_doc ON warehouse_issues(document_id,resolved_at);
            CREATE TABLE IF NOT EXISTS warehouse_returns (
              id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
              issue_id TEXT NOT NULL REFERENCES warehouse_issues(id),request_id TEXT NOT NULL,
              message TEXT NOT NULL,created_at TEXT NOT NULL,acknowledged_at TEXT,
              UNIQUE(document_id,request_id));
            CREATE INDEX IF NOT EXISTS warehouse_returns_doc ON warehouse_returns(document_id,acknowledged_at);
            ''')
            c.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in c.execute('PRAGMA table_info(warehouse_documents)')}
            if 'revision' not in columns:
                c.execute('ALTER TABLE warehouse_documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            if 'deleted_at' not in columns:
                c.execute('ALTER TABLE warehouse_documents ADD COLUMN deleted_at TEXT')
            if 'archived_at' not in columns:
                c.execute('ALTER TABLE warehouse_documents ADD COLUMN archived_at TEXT')
            if 'gtd_required' not in columns:
                c.execute('ALTER TABLE warehouse_documents ADD COLUMN gtd_required INTEGER NOT NULL DEFAULT 0')
            if 'gtd_state' not in columns:
                c.execute("ALTER TABLE warehouse_documents ADD COLUMN gtd_state TEXT NOT NULL DEFAULT 'none'")
            if 'gtd_message' not in columns:
                c.execute("ALTER TABLE warehouse_documents ADD COLUMN gtd_message TEXT NOT NULL DEFAULT ''")
            if 'sales_username' not in columns:
                c.execute('ALTER TABLE warehouse_documents ADD COLUMN sales_username TEXT')
            c.execute('''CREATE INDEX IF NOT EXISTS warehouse_sales_tasks
                ON warehouse_documents(sales_username, created_at DESC) WHERE sales_username IS NOT NULL''')
            c.commit()
            self.init_settings(c)
            self.init_upd(c)
            self.init_gtd_excel(c)
            self.init_receipt_bridge(c)
        c.close()

    @contextmanager
    def connection(self, write=False):
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=15000')
        try:
            c.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _doc(c, document_id, include_deleted=False):
        row = c.execute('SELECT * FROM warehouse_documents WHERE id=?', (document_id,)).fetchone()
        if not row or (row['deleted_at'] and not include_deleted):
            raise WarehouseError('Документ не найден', 404)
        return dict(row)

    @staticmethod
    def _active(d):
        if d['archived_at']:
            raise WarehouseError('Документ находится в архиве', 409)
        if d['status'] != 'active':
            raise WarehouseError('Документ не запущен или уже завершён', 409)

    @staticmethod
    def _bump(c, document_id):
        c.execute('UPDATE warehouse_documents SET version=version+1, revision=revision+1, updated_at=? WHERE id=?',
                  (now(), document_id))


    def signer_job_create(self, inn, challenge_uuid, challenge_data):
        job_id = uuid.uuid4().hex
        digest = hashlib.sha256(challenge_data.encode('utf-8')).hexdigest()
        with self.connection(True) as c:
            c.execute('''INSERT INTO gis_signer_jobs
                (id,inn,challenge_uuid,challenge_data,sha256,state,created_at,updated_at)
                VALUES (?,?,?,?,?,'queued',?,?)''',
                (job_id, inn, challenge_uuid, challenge_data, digest, now(), now()))
        return self.signer_job_get(job_id)

    def signer_job_get(self, job_id):
        with self.connection() as c:
            row = c.execute('''SELECT id,inn,state,error,created_at,updated_at
                               FROM gis_signer_jobs WHERE id=?''', (job_id,)).fetchone()
            if row is None:
                raise WarehouseError('Задание подписи не найдено', 404)
            return dict(row)

    def signer_next(self, thumbprint):
        with self.connection(True) as c:
            ts = now()
            c.execute("""UPDATE gis_signer_jobs SET state='queued',lease=NULL,lease_until=NULL,updated_at=?
                         WHERE state='leased' AND lease_until<?""", (ts, time.time()))
            row = c.execute("SELECT * FROM gis_signer_jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            lease = secrets.token_urlsafe(32)
            c.execute("UPDATE gis_signer_jobs SET state='leased',lease=?,lease_until=?,updated_at=? WHERE id=?",
                      (lease, time.time() + 180, ts, row['id']))
            return dict(id=row['id'], kind='auth_true', lease=lease,
                        payload_base64=base64.b64encode(row['challenge_data'].encode('utf-8')).decode('ascii'),
                        sha256=row['sha256'], detached=False, thumbprint=thumbprint, inn=row['inn'])

    def signer_claim_result(self, job_id, lease, digest):
        with self.connection(True) as c:
            row = c.execute('SELECT * FROM gis_signer_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None or row['state'] != 'leased':
                raise WarehouseError('Задание уже обработано или отозвано', 409)
            if (row['lease_until'] or 0) < time.time() or not hmac.compare_digest(row['lease'] or '', lease):
                raise WarehouseError('Истёк срок задания подписи', 409)
            if not hmac.compare_digest(row['sha256'], digest):
                raise WarehouseError('Подписаны другие байты', 409)
            c.execute("UPDATE gis_signer_jobs SET state='signing',lease=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                      (now(), job_id))
            return dict(row)

    def signer_job_finish(self, job_id, state, error=''):
        with self.connection(True) as c:
            c.execute('UPDATE gis_signer_jobs SET state=?,error=?,updated_at=? WHERE id=?',
                      (state, error[:1000], now(), job_id))
        return self.signer_job_get(job_id)

    def signer_heartbeat(self, thumbprint, expires, status):
        with self.connection(True) as c:
            c.execute('INSERT OR REPLACE INTO gis_signer_heartbeat VALUES (1,?,?,?,?)',
                      (thumbprint, expires, status, now()))

    def create(self, header, lines, expected, *, sales_username=None):
        document_id, ts = uuid.uuid4().hex, now()
        with self.connection(True) as c:
            if sales_username is not None and (
                    header['kind'] != 'shipment' or not any(line['gtin'] for line in lines)):
                raise WarehouseError('Для задания нужна хотя бы одна маркируемая позиция с GTIN')
            if header['kind'] == 'shipment':
                shipment_date = date.fromisoformat(header['date'])
                existing = c.execute("""SELECT date FROM warehouse_documents
                    WHERE kind='shipment' AND org_inn=? AND number=?
                    AND (sales_username IS NOT NULL OR ? IS NOT NULL)""",
                    (header['org_inn'], header['number'], sales_username))
                if any(date.fromisoformat(row['date']) == shipment_date for row in existing):
                    raise WarehouseError(
                        'Накладная с таким номером и датой уже есть. Для изменений обратитесь к ответственному.',
                        409, 'shipment_exists')
                if sales_username is not None:
                    header = {**header, 'date': shipment_date.isoformat()}
            header = {**header, 'strict_owner': header.get('strict_owner', self._settings(c)['values']['strict_owner_default'])}
            c.execute('''INSERT INTO warehouse_documents
                (id,kind,number,date,org_inn,org_name,recipient_name,recipient_inn,
                 strict_owner,created_at,updated_at,sales_username) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (document_id, header['kind'], header['number'], header['date'],
                 header['org_inn'], header['org_name'], header.get('recipient_name', ''),
                 header.get('recipient_inn', ''), int(header['strict_owner']), ts, ts, sales_username))
            for line in lines:
                if not line['gtin']:
                    c.execute('INSERT INTO warehouse_unmarked_lines VALUES (?,?,?,?)',
                              (uuid.uuid4().hex, document_id, line['pn'], line['quantity']))
                    continue
                c.execute('INSERT INTO warehouse_lines VALUES (?,?,?,?,?)',
                          (document_id, line['gtin'], line['pn'], line['pn_source'], line['quantity']))
            for e in expected:
                c.execute('INSERT INTO warehouse_expected VALUES (?,?,?,?,?,?)',
                          (document_id, e['cis'], e['raw_code'], e['full_code'], e['gtin'], e['quantity']))
            actor = 'sales' if sales_username is not None else 'admin'
            details = {'number': header['number'], 'kind': header['kind']}
            if sales_username is not None:
                details['username'] = sales_username
            self._event(c, document_id, 'created', details, actor=actor)
            if sales_username is not None:
                self._start(c, document_id, actor=actor)
                return {'number': header['number']}
        return self.get(document_id)

    def append_codes(self, document_id, header, lines, expected):
        """Append only new CIS, without changing scans or historical checks."""
        with self.connection(True) as c:
            d = self._doc(c, document_id)
            if d['archived_at']:
                raise WarehouseError('Сначала верните документ из архива', 409)
            if d['check_id'] and (d['check_until'] or 0) > time.time():
                raise WarehouseError('Идёт проверка КМ; повторите загрузку позже', 409)
            if any(header[k] != d[k] for k in ('number', 'date', 'org_inn')):
                raise WarehouseError('Реквизиты файла не совпадают с документом', 409)
            if c.execute('SELECT 1 FROM warehouse_receipt_sources WHERE document_id=?',
                         (document_id,)).fetchone():
                raise WarehouseError('Приёмка управляется marking-bm; дополните исходную накладную', 409)
            if any(not line['gtin'] for line in lines) or not expected:
                raise WarehouseError('Для дозагрузки нужны только строки с КМ')
            known = {r['cis'] for r in c.execute(
                'SELECT cis FROM warehouse_expected WHERE document_id=?', (document_id,))}
            if d['kind'] == 'shipment':
                known.update(r['cis'] for r in c.execute(
                    'SELECT cis FROM warehouse_scans WHERE document_id=?', (document_id,)))
            pns = {line['gtin']: line['pn'] for line in lines}
            additions = {}
            added = 0
            for e in expected:
                if e['cis'] in known:
                    continue
                known.add(e['cis'])
                gtin = e['gtin']
                additions[gtin] = additions.get(gtin, 0) + e['quantity']
                c.execute('INSERT INTO warehouse_expected VALUES (?,?,?,?,?,?)',
                          (document_id, e['cis'], e['raw_code'], e['full_code'], gtin, e['quantity']))
                added += 1
            for gtin, quantity in additions.items():
                prior = c.execute('SELECT * FROM warehouse_lines WHERE document_id=? AND gtin=?',
                                  (document_id, gtin)).fetchone()
                if quantity + (prior['quantity'] if prior else 0) > 2147483647:
                    raise WarehouseError('Слишком большое количество товара')
                if prior:
                    if prior['pn'] and pns[gtin] and prior['pn'] != pns[gtin]:
                        raise WarehouseError('У одного GTIN указаны разные PN')
                    c.execute('UPDATE warehouse_lines SET quantity=quantity+? WHERE document_id=? AND gtin=?',
                              (quantity, document_id, gtin))
                else:
                    c.execute('INSERT INTO warehouse_lines VALUES (?,?,?,?,?)',
                              (document_id, gtin, pns[gtin], 'excel' if pns[gtin] else '', quantity))
            if added:
                if d['status'] == 'completed':
                    self._start(c, document_id, reopen=True)
                else:
                    self._bump(c, document_id)
                self._event(c, document_id, 'codes_appended',
                            {'added': added, 'duplicates': len(expected)-added, 'quantities': additions}, actor='admin')
            return {**self._snapshot(c, document_id), 'append_summary': {
                'added': added, 'duplicates': len(expected)-added, 'quantity': sum(additions.values())}}

    @staticmethod
    def _replace_shipment_gtd(c, document_id, source_items=None, gtd_rows=None, gtd_state=None, gtd_message=None):
        changed = False
        source_changed = False
        if source_items is not None:
            old = [tuple(r) for r in c.execute(
                'SELECT good_id,gtin,pn,quantity FROM warehouse_shipment_items WHERE document_id=? ORDER BY good_id,gtin',
                (document_id,))]
            new = sorted((str(x['good_id']), str(x['gtin']), str(x['pn']), int(x['quantity']))
                         for x in source_items)
            if old != new:
                c.execute('DELETE FROM warehouse_shipment_items WHERE document_id=?', (document_id,))
                c.executemany('INSERT INTO warehouse_shipment_items VALUES (?,?,?,?,?)',
                              [(document_id, *item) for item in new])
                changed = True
                source_changed = True
            current = c.execute('SELECT gtd_required FROM warehouse_documents WHERE id=?', (document_id,)).fetchone()
            if current and not current['gtd_required']:
                c.execute("""UPDATE warehouse_documents SET gtd_required=1,gtd_state='pending',
                             gtd_message='Ожидается загрузка разбивки строк по ГТД' WHERE id=?""", (document_id,))
                changed = True
            elif source_changed and gtd_state is None:
                c.execute('DELETE FROM warehouse_gtd_rows WHERE document_id=?', (document_id,))
                c.execute("""UPDATE warehouse_documents SET gtd_required=1,gtd_state='pending',
                             gtd_message='Состав GoodId изменился. Загрузите актуальную разбивку строк по ГТД' WHERE id=?""",
                          (document_id,))
                changed = True
        if gtd_state is not None:
            old = [tuple(r) for r in c.execute(
                'SELECT position,good_id,gtin,quantity,declaration,source_sheet,source_row FROM warehouse_gtd_rows WHERE document_id=? ORDER BY position',
                (document_id,))]
            new = [(str(x['good_id']), str(x['gtin']), int(x['quantity']), str(x['declaration']),
                    str(x.get('source_sheet') or ''), int(x.get('source_row') or 0)) for x in (gtd_rows or [])]
            current = c.execute('SELECT gtd_required,gtd_state,gtd_message FROM warehouse_documents WHERE id=?',
                                (document_id,)).fetchone()
            if old != new or not current['gtd_required'] or current['gtd_state'] != gtd_state or current['gtd_message'] != (gtd_message or ''):
                c.execute('DELETE FROM warehouse_gtd_rows WHERE document_id=?', (document_id,))
                c.executemany('INSERT INTO warehouse_gtd_rows VALUES (?,?,?,?,?,?,?,?)',
                              [(document_id, index, *item) for index, item in enumerate(new, 1)])
                c.execute('UPDATE warehouse_documents SET gtd_required=1,gtd_state=?,gtd_message=? WHERE id=?',
                          (gtd_state, gtd_message or '', document_id))
                changed = True
        return changed

    def sync_shipment_invoice(self, invoice_id, header, lines, *, source_items=None, gtd_rows=None, gtd_state=None, gtd_message=None):
        """One invoice per write transaction: scan/check operations serialize with sync."""
        with self.connection(True) as c:
            source = c.execute('SELECT document_id FROM warehouse_shipment_sources WHERE org_inn=? AND invoice_id=?',
                               (header['org_inn'], invoice_id)).fetchone()
            document_id = source['document_id'] if source else None
            if not document_id:
                # Older manual/sales tasks have no InvoiceId. Adopt only an unambiguous number.
                matches = list(c.execute("SELECT id FROM warehouse_documents WHERE kind='shipment' AND org_inn=? AND number=?",
                                         (header['org_inn'], header['number'])))
                if len(matches) > 1:
                    raise WarehouseError('Несколько заданий с этим номером: требуется разбор администратора', 409)
                if matches:
                    document_id = matches[0]['id']
                    if c.execute('SELECT 1 FROM warehouse_shipment_sources WHERE document_id=?', (document_id,)).fetchone():
                        raise WarehouseError('Номер уже связан с другим InvoiceId', 409)
            d = self._snapshot(c, document_id, include_deleted=True) if document_id else None
            if d and d['deleted_at']:
                raise WarehouseError('Задание удалено; сначала восстановите его вручную', 409)

            recipient_inn = header['recipient_inn']

            if not document_id:
                if not any(line['gtin'] for line in lines):
                    return dict(status='skipped', message='Нет маркируемых позиций')
                document_id, ts = uuid.uuid4().hex, now()
                c.execute("""INSERT INTO warehouse_documents
                    (id,kind,number,date,org_inn,org_name,recipient_name,recipient_inn,
                     strict_owner,created_at,updated_at) VALUES (?,'shipment',?,?,?,?,?,?,?,?,?)""",
                    (document_id, header['number'], header['date'], header['org_inn'], header['org_name'],
                     header.get('recipient_name', ''), recipient_inn, int(self._settings(c)['values']['strict_owner_default']), ts, ts))
                self._event(c, document_id, 'created', {'source': 'invoice_list', 'invoice_id': invoice_id})
                outcome = 'created'
            else:
                canonical = lambda items: sorted((l['gtin'], l['pn'], l['quantity']) for l in items)
                same = (canonical(lines) == canonical(d['lines']) and d['number'] == header['number']
                        and d['recipient_inn'] == recipient_inn)
                if same:
                    metadata_changed = self._replace_shipment_gtd(
                        c, document_id, source_items, gtd_rows, gtd_state, gtd_message)
                    recipient_name = header.get('recipient_name')
                    if recipient_name and recipient_name != d['recipient_name']:
                        c.execute('UPDATE warehouse_documents SET recipient_name=? WHERE id=?',
                                  (recipient_name, document_id))
                        metadata_changed = True
                    if not source:
                        c.execute('INSERT INTO warehouse_shipment_sources VALUES (?,?,?)',
                                  (header['org_inn'], invoice_id, document_id))
                    if metadata_changed:
                        self._bump(c, document_id)
                        self._event(c, document_id, 'shipment_gtd_updated',
                                    {'gtd_state': gtd_state, 'gtd_message': gtd_message}, actor='admin')
                    current = self._snapshot(c, document_id)
                    return dict(status='updated' if metadata_changed else 'unchanged',
                                document_id=document_id, gtd_state=current['gtd_state'],
                                message='Данные ГТД обновлены; требуется повторная проверка' if metadata_changed
                                else 'Без изменений; сканы и статус сохранены')
                if d['archived_at']:
                    raise WarehouseError('Состав архивного задания изменён; сначала верните его из архива', 409)
                if d['checking']:
                    raise WarehouseError('Идёт проверка КМ; повторите загрузку после её завершения', 409)
                outcome = 'updated'
                self._event(c, document_id, 'invoice_synced',
                            {'invoice_id': invoice_id, 'before': d['lines'], 'after': lines,
                             'recipient_before': d['recipient_inn'], 'recipient_after': recipient_inn,
                             'number_before': d['number'], 'number_after': header['number']})
                c.execute('DELETE FROM warehouse_lines WHERE document_id=?', (document_id,))
                c.execute('DELETE FROM warehouse_unmarked_lines WHERE document_id=?', (document_id,))
            for line in lines:
                if line['gtin']:
                    c.execute('INSERT INTO warehouse_lines VALUES (?,?,?,?,?)',
                              (document_id, line['gtin'], line['pn'], 'excel', line['quantity']))
                else:
                    c.execute('INSERT INTO warehouse_unmarked_lines VALUES (?,?,?,?)',
                              (uuid.uuid4().hex, document_id, line['pn'], line['quantity']))
            self._replace_shipment_gtd(
                c, document_id, source_items, gtd_rows,
                gtd_state if gtd_state is not None else 'pending',
                gtd_message if gtd_message is not None else 'Ожидается загрузка разбивки строк по ГТД')
            if not source:
                c.execute('INSERT INTO warehouse_shipment_sources VALUES (?,?,?)',
                          (header['org_inn'], invoice_id, document_id))
            c.execute("""UPDATE warehouse_documents SET number=?,
                recipient_name=CASE WHEN ? != '' THEN ? WHEN recipient_inn=? THEN recipient_name ELSE '' END,
                recipient_inn=?,status='active',
                started_at=COALESCE(started_at,?),completed_at=NULL,result=NULL,result_version=NULL,
                result_at=NULL,check_id=NULL,check_until=NULL WHERE id=?""",
                (header['number'], header.get('recipient_name', ''), header.get('recipient_name', ''),
                 recipient_inn, recipient_inn, now(), document_id))
            self._bump(c, document_id)
            return dict(status=outcome, document_id=document_id,
                        message='Задание создано' if outcome == 'created' else 'Состав обновлён; сканы сохранены, требуется проверка')

    def sales_shipments(self, username):
        with self.connection() as c:
            return [dict(row) for row in c.execute('''SELECT d.number, d.date, d.org_name,
                d.recipient_name, d.status, d.updated_at, d.completed_at, d.archived_at, d.deleted_at,
                (SELECT count(*) FROM warehouse_scans s WHERE s.document_id=d.id) AS scan_count,
                EXISTS(SELECT 1 FROM warehouse_issues i
                       WHERE i.document_id=d.id AND i.resolved_at IS NULL) AS has_open_issue
                FROM warehouse_documents d WHERE d.sales_username=? AND d.kind='shipment'
                ORDER BY d.created_at DESC, d.id''', (username,))]

    def _snapshot(self, c, document_id, include_deleted=False):
        d = self._doc(c, document_id, include_deleted)
        d['awaiting_invoice_inn'] = self._awaiting_invoice_inn(d)
        d.pop('recipient_choice_key', None)
        self._issue_counts(c, d)
        source = c.execute('SELECT metadata,revision FROM warehouse_receipt_sources WHERE document_id=?', (document_id,)).fetchone()
        d['receipt_source'] = {**json.loads(source['metadata']), 'revision': source['revision']} if source else None
        d['lines'] = [dict(r) for r in c.execute(
            'SELECT gtin,pn,pn_source,quantity FROM warehouse_lines WHERE document_id=? ORDER BY gtin', (document_id,))]
        d['gtd_required'] = bool(d.get('gtd_required'))
        d['gtd_rows'] = [dict(r) for r in c.execute(
            'SELECT good_id,gtin,quantity,declaration,source_sheet,source_row FROM warehouse_gtd_rows WHERE document_id=? ORDER BY position',
            (document_id,))]
        d['shipment_items'] = [dict(r) for r in c.execute(
            'SELECT good_id,gtin,pn,quantity FROM warehouse_shipment_items WHERE document_id=? ORDER BY good_id,gtin',
            (document_id,))]
        d['splits'] = []
        for split in c.execute(
                'SELECT id,source_scan_id,source_cis,parts,external_batch_id,expected_codes,state,error,created_at,updated_at '
                'FROM warehouse_splits WHERE document_id=? ORDER BY created_at,id', (document_id,)):
            item = dict(split)
            item['parts'] = json.loads(item['parts'])
            item['expected_codes'] = json.loads(item['expected_codes'])
            d['splits'].append(item)
        d['lines'] += [dict(r) for r in c.execute(
            "SELECT id AS line_id,'' AS gtin,pn,'excel' AS pn_source,quantity FROM warehouse_unmarked_lines WHERE document_id=? ORDER BY rowid",
            (document_id,))]
        d['scans'] = [dict(r) for r in c.execute(
            'SELECT * FROM warehouse_scans WHERE document_id=? ORDER BY scanned_at,id', (document_id,))]
        d['expected'] = [dict(r) for r in c.execute(
            'SELECT * FROM warehouse_expected WHERE document_id=? ORDER BY cis', (document_id,))]
        d['result'] = json.loads(d['result']) if d['result'] else None
        d['runtime_settings'] = self._settings(c)
        d['checking'] = bool(d['check_id'] and (d['check_until'] or 0) > time.time())
        d['check_current'] = _check_current(d)
        split_blocked = any(item['state'] != 'done' for item in d['splits'])
        gtd_ok = (not d['gtd_required'] or
                  (d['gtd_state'] == 'ready' and d['result'] and (d['result'].get('gtd') or {}).get('ok')))
        d['can_complete'] = (d['status'] == 'active' and not d['deleted_at'] and not d['archived_at'] and not d['checking'] and
                             d['check_current'] and d['result']['ok'] and gtd_ok and not split_blocked)
        d.pop('check_id', None)
        d.pop('check_until', None)
        return d

    def get(self, document_id, include_deleted=False):
        with self.connection() as c:
            return self._snapshot(c, document_id, include_deleted)

    def list(self, admin=False, kind=None, org_inn=None, scan_state='all', status=None,
             query='', date_from=None, date_to=None, issue_state='all', trash=False, archived=False):
        with self.connection() as c:
            c.create_function('casefold', 1, lambda value: (value or '').casefold(), deterministic=True)
            if admin and trash:
                section = 'deleted_at IS NOT NULL'
            elif admin and archived:
                section = 'deleted_at IS NULL AND archived_at IS NOT NULL'
            else:
                section = 'deleted_at IS NULL AND archived_at IS NULL'
            clauses, args = [section], []
            if not admin:
                clauses.append("status = 'active'")
            for field, value in [('kind',kind),('org_inn',org_inn),('status',status)]:
                if value:
                    clauses.append(field+'=?'); args.append(value)
            if scan_state != 'all':
                clauses.append('scan_count=0' if scan_state=='unscanned' else 'scan_count>0')
            if query.strip():
                if admin:
                    clauses.append("instr(casefold(number || ' ' || recipient_name || ' ' || recipient_inn || ' ' || org_name || ' ' || org_inn),?)>0")
                else:
                    clauses.append('instr(casefold(number),?)>0')
                args.append(query.strip().casefold())
            if date_from:
                clauses.append('date>=?'); args.append(str(date_from))
            if date_to:
                clauses.append('date<=?'); args.append(str(date_to))
            if admin:
                if issue_state != 'all':
                    clauses.append({'open':'open_issues>0','closed':'issue_count>0 AND open_issues=0','none':'issue_count=0'}[issue_state])
            sql = '''SELECT * FROM (SELECT d.*,
                (SELECT count(*) FROM warehouse_scans s WHERE s.document_id=d.id) AS scan_count,
                (SELECT count(*) FROM warehouse_issues i WHERE i.document_id=d.id) AS issue_count,
                (SELECT count(*) FROM warehouse_issues i WHERE i.document_id=d.id AND i.resolved_at IS NULL) AS open_issues
                ,(SELECT count(*) FROM warehouse_returns r WHERE r.document_id=d.id AND r.acknowledged_at IS NULL) AS pending_returns
                FROM warehouse_documents d) WHERE '''
            rows = c.execute(sql + ' AND '.join(clauses) + ' ORDER BY pending_returns>0 DESC,open_issues>0 DESC,created_at DESC,id', args)
            result = []
            for r in rows:
                d = dict(r)
                d['awaiting_invoice_inn'] = self._awaiting_invoice_inn(d)
                d.pop('recipient_choice_key', None)
                d['result'] = json.loads(d['result']) if d['result'] else None
                d['check_current'] = _check_current(d)
                d.pop('check_id', None); d.pop('check_until', None)
                result.append(d)
            return result

    def delete(self, document_id, version, revision):
        """Remove from active use, retaining the document and audit data in SQLite."""
        with self.connection(True) as c:
            d = self._doc(c, document_id)
            if d['archived_at']:
                raise WarehouseError('Сначала верните документ из архива', 409)
            if d['version'] != version or d['revision'] != revision:
                raise WarehouseError('Документ изменён другим сотрудником. Обновите карточку и повторите удаление.', 409)
            if d['check_id'] and (d['check_until'] or 0) > time.time():
                raise WarehouseError('Дождитесь завершения проверки перед удалением документа', 409)
            c.execute('''UPDATE warehouse_documents SET deleted_at=?,updated_at=?,
                version=version+1,revision=revision+1 WHERE id=?''', (now(), now(), document_id))
            self._event(c, document_id, 'deleted', actor='admin')

    def start(self, document_id, reopen=False):
        with self.connection(True) as c:
            self._start(c, document_id, reopen)
        return self.get(document_id)

    @staticmethod
    def _awaiting_invoice_inn(d):
        # Drafts from the former catalog workflow must receive their INN by reimport.
        return bool(d.get('recipient_choice_key') and not d['recipient_inn'])

    def _start(self, c, document_id, reopen=False, actor='admin'):
        d = self._doc(c, document_id)
        if d['archived_at']:
            raise WarehouseError('Сначала верните документ из архива', 409)
        if d['status'] != ('completed' if reopen else 'draft'):
            raise WarehouseError('Недопустимый переход статуса', 409)
        if self._awaiting_invoice_inn(d):
            raise WarehouseError('Повторно загрузите накладную из выгрузки с ИНН получателя', 409)
        c.execute('''UPDATE warehouse_documents SET status='active',started_at=?,
            completed_at=NULL, version=version+1, revision=revision+1, updated_at=? WHERE id=?''',
            (now(), now(), document_id))
        self._event(c, document_id, 'reopened' if reopen else 'started', actor=actor)

    def add_scan(self, document_id, scan):
        with self.connection(True) as c:
            self._active(self._doc(c, document_id))
            existing = c.execute('SELECT * FROM warehouse_scans WHERE document_id=? AND cis=?',
                                 (document_id, scan['cis'])).fetchone()
            if existing:
                return {'duplicate': True, 'scan': dict(existing)}
            if not c.execute('SELECT 1 FROM warehouse_lines WHERE document_id=? AND gtin=?',
                             (document_id, scan['gtin'])).fetchone():
                raise WarehouseError(
                    f"GTIN {scan['gtin']} отсутствует в составе документа. "
                    'КМ не добавлен. Проверьте товар и продолжайте сканирование.',
                    code='gtin_not_in_document')
            row = {**scan, 'id': uuid.uuid4().hex, 'scanned_at': now()}
            c.execute('INSERT INTO warehouse_scans VALUES (?,?,?,?,?,?,?,?)',
                      (row['id'], document_id, row['cis'], row['raw_code'], row['full_code'],
                       row['gtin'], int(row['has_crypto']), row['scanned_at']))
            self._bump(c, document_id)
            self._event(c, document_id, 'scan_added', row)
            return {'duplicate': False, 'scan': row}

    def delete_scan(self, document_id, scan_id):
        with self.connection(True) as c:
            self.require_feature(c, 'worker_delete_scans')
            self._active(self._doc(c, document_id))
            prior = c.execute('SELECT * FROM warehouse_scans WHERE document_id=? AND id=?', (document_id,scan_id)).fetchone()
            changed = c.execute('DELETE FROM warehouse_scans WHERE document_id=? AND id=?',
                                (document_id, scan_id)).rowcount
            if changed:
                self._bump(c, document_id)
                self._event(c, document_id, 'scan_removed', dict(prior))
        return self.get(document_id)

    @staticmethod
    def _split_state(value):
        return {'creating':'ordering','ordering':'ordering','ready_to_rescan':'ready_to_rescan',
                'reporting':'reporting','done':'done','failed':'failed'}.get(value, 'unknown')

    def create_split(self, document_id, scan_id, request_id, parts, external):
        with self.connection(True) as c:
            self._active(self._doc(c, document_id))
            existing = c.execute('SELECT * FROM warehouse_splits WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                return dict(existing)
            prior = c.execute('SELECT * FROM warehouse_scans WHERE document_id=? AND id=?',
                              (document_id, scan_id)).fetchone()
            if prior is None:
                raise WarehouseError('Исходный скан уже изменён. Обновите задание и повторите проверку.', 409)
            split_id = uuid.uuid4().hex
            expected = external.get('expected_codes') or []
            state = self._split_state(external.get('state'))
            c.execute('DELETE FROM warehouse_scans WHERE document_id=? AND id=?', (document_id, scan_id))
            c.execute('''INSERT INTO warehouse_splits
                (id,document_id,source_scan_id,request_id,source_cis,parts,external_batch_id,
                 expected_codes,state,error,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (split_id, document_id, scan_id, request_id, prior['cis'],
                 json.dumps(parts), external.get('id'),
                 json.dumps(expected, ensure_ascii=False), state, external.get('error') or '',
                 now(), now()))
            self._bump(c, document_id)
            self._event(c, document_id, 'scan_removed_for_gtd_split',
                        {'scan':dict(prior), 'split_id':split_id, 'parts':parts,
                         'external_batch_id':external.get('id'), 'state':state})
        return self.get(document_id)

    def update_split(self, document_id, split_id, external):
        state = self._split_state(external.get('state'))
        expected = external.get('expected_codes') or []
        with self.connection(True) as c:
            row = c.execute('SELECT * FROM warehouse_splits WHERE document_id=? AND id=?',
                            (document_id, split_id)).fetchone()
            if row is None:
                raise WarehouseError('Операция разделения не найдена', 404)
            before = (row['state'], row['expected_codes'], row['error'])
            after = (state, json.dumps(expected, ensure_ascii=False), external.get('error') or '')
            c.execute('''UPDATE warehouse_splits SET state=?,expected_codes=?,error=?,
                         external_batch_id=COALESCE(?,external_batch_id),updated_at=? WHERE id=?''',
                      (state, after[1], after[2], external.get('id'), now(), split_id))
            if before != after:
                self._event(c, document_id, 'gtd_split_state',
                            {'split_id':split_id, 'before':before[0], 'after':state,
                             'error':after[2]})
        return self.get(document_id)

    def split_record(self, document_id, split_id):
        with self.connection() as c:
            row = c.execute('SELECT * FROM warehouse_splits WHERE document_id=? AND id=?',
                            (document_id, split_id)).fetchone()
            if row is None:
                raise WarehouseError('Операция разделения не найдена', 404)
            result = dict(row)
            result['parts'] = json.loads(result['parts'])
            result['expected_codes'] = json.loads(result['expected_codes'])
            return result

    def edit(self, document_id, version, strict_owner=None, pns=None, quantities=None, revision=None):
        with self.connection(True) as c:
            d = self._doc(c, document_id)
            if d['archived_at']:
                raise WarehouseError('Сначала верните документ из архива', 409)
            if d['version'] != version or (revision is not None and d['revision'] != revision):
                raise WarehouseError('Документ изменён другим сотрудником. Обновите карточку.', 409)
            # PN may be supplied before export even after completion. It does not affect reconciliation.
            if strict_owner is not None:
                if d['status'] == 'completed':
                    raise WarehouseError('Сначала возобновите документ', 409)
                c.execute('UPDATE warehouse_documents SET strict_owner=? WHERE id=?', (int(strict_owner), document_id))
                self._bump(c, document_id)
            if quantities:
                if d['kind'] != 'shipment' or d['status'] == 'completed':
                    raise WarehouseError('План можно менять только для незавершённой отгрузки', 409)
                for gtin, quantity in quantities.items():
                    if type(quantity) is not int or not 0 < quantity <= 2147483647:
                        raise WarehouseError('Плановое количество должно быть положительным целым числом')
                    if not c.execute('UPDATE warehouse_lines SET quantity=? WHERE document_id=? AND gtin=?',
                                     (quantity, document_id, gtin)).rowcount:
                        raise WarehouseError('GTIN отсутствует в документе')
                self._bump(c, document_id)
            for gtin, pn in (pns or {}).items():
                if not c.execute('UPDATE warehouse_lines SET pn=?,pn_source=? WHERE document_id=? AND gtin=?',
                                 (pn.strip(), 'manual', document_id, gtin)).rowcount:
                    raise WarehouseError('GTIN отсутствует в документе')
            c.execute('UPDATE warehouse_documents SET revision=revision+1,updated_at=? WHERE id=?', (now(), document_id))
            self._event(c, document_id, 'edited', {'pns':pns,'quantities':quantities,'strict_owner':strict_owner}, actor='admin')
        return self.get(document_id)

    def begin_check(self, document_id):
        with self.connection(True) as c:
            d = self._doc(c, document_id)
            self._active(d)
            if d['check_id'] and (d['check_until'] or 0) > time.time():
                raise WarehouseError('Проверка уже выполняется', 409)
            check_id = uuid.uuid4().hex
            c.execute('UPDATE warehouse_documents SET check_id=?,check_until=? WHERE id=?',
                      (check_id, time.time() + 600, document_id))
            self._event(c, document_id, 'check_started', {'version':d['version']})
            return check_id, self._snapshot(c, document_id)

    def finish_check(self, document_id, check_id, version, result):
        with self.connection(True) as c:
            d = self._doc(c, document_id)
            body = json.dumps(result, ensure_ascii=False)
            c.execute('INSERT INTO warehouse_checks VALUES (?,?,?,?,?)',
                      (check_id, document_id, version, now(), body))
            if d['check_id'] == check_id:
                c.execute('''UPDATE warehouse_documents SET check_id=NULL,check_until=NULL,
                    result=?, result_version=?,result_at=?,updated_at=? WHERE id=?''',
                    (body, version, now(), now(), document_id))
            self._event(c, document_id, 'checked', {'version':version,'ok':result['ok'],'current':d['version']==version})
        return self.get(document_id)

    def cancel_check(self, document_id, check_id):
        with self.connection(True) as c:
            c.execute('UPDATE warehouse_documents SET check_id=NULL,check_until=NULL WHERE id=? AND check_id=?',
                      (document_id, check_id))

    def fill_pns(self, document_id, pns):
        with self.connection(True) as c:
            d = self._doc(c, document_id)
            if d['archived_at']:
                raise WarehouseError('Документ находится в архиве', 409)
            changed = 0
            for gtin, pn in pns.items():
                changed += c.execute("UPDATE warehouse_lines SET pn=?,pn_source='trueapi' WHERE document_id=? AND gtin=? AND pn=''",
                                     (pn, document_id, gtin)).rowcount
            if changed:
                c.execute('UPDATE warehouse_documents SET revision=revision+1,updated_at=? WHERE id=?', (now(), document_id))

    def complete(self, document_id):
        with self.connection(True) as c:
            d = self._snapshot(c, document_id)
            if not d['can_complete']:
                raise WarehouseError('Завершение запрещено: выполните актуальную проверку без расхождений', 409)
            c.execute("UPDATE warehouse_documents SET status='completed',revision=revision+1,completed_at=?,updated_at=? WHERE id=?",
                      (now(), now(), document_id))
            self._event(c, document_id, 'completed')
        return self.get(document_id)

    def archive(self, document_id, version, revision, confirmation):
        """Archive a completed, error-free document without external GIS MT changes."""
        with self.connection(True) as c:
            d = self._snapshot(c, document_id)
            if confirmation != d['number']:
                raise WarehouseError('Для подтверждения введите точный номер накладной')
            if d['version'] != version or d['revision'] != revision:
                raise WarehouseError('Документ изменён. Обновите карточку.', 409)
            if d['archived_at']:
                raise WarehouseError('Документ уже находится в архиве', 409)
            result = d['result']
            if (d['status'] != 'completed' or not d['check_current'] or d['checking']
                    or not result or not result.get('ok')
                    or result.get('missing')
                    or any(code.get('errors') for code in result.get('codes', []))
                    or d['open_issues'] or d['pending_returns']):
                raise WarehouseError('Архив доступен только для завершённого документа без расхождений и открытых обращений', 409)
            ts = now()
            c.execute('''UPDATE warehouse_documents SET archived_at=?, revision=revision+1,
                         updated_at=? WHERE id=?''', (ts, ts, document_id))
            self._event(c, document_id, 'archived', actor='admin')
        return self.get(document_id)

    def unarchive(self, document_id, version, revision, confirmation):
        """Restore to completed documents, leaving reopening for scans explicit."""
        with self.connection(True) as c:
            d = self._doc(c, document_id)
            if confirmation != d['number']:
                raise WarehouseError('Для подтверждения введите точный номер накладной')
            if d['version'] != version or d['revision'] != revision:
                raise WarehouseError('Документ изменён. Обновите карточку.', 409)
            if not d['archived_at']:
                raise WarehouseError('Документ не находится в архиве', 409)
            ts = now()
            c.execute('''UPDATE warehouse_documents SET archived_at=NULL, revision=revision+1,
                         updated_at=? WHERE id=?''', (ts, document_id))
            self._event(c, document_id, 'unarchived', actor='admin')
        return self.get(document_id)
