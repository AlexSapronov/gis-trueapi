"""Durable document state. Short SQLite transactions; no network inside locks."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from warehouse_issues import WarehouseIssues
from app_settings import RuntimeSettings


class WarehouseError(Exception):
    def __init__(self, message, status=422, code=None):
        self.message, self.status = message, status
        self.code = code
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat()


class WarehouseStore(WarehouseIssues, RuntimeSettings):
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
              result_version INTEGER, result_at TEXT, deleted_at TEXT, archived_at TEXT);
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
            if 'revision' not in {r[1] for r in c.execute('PRAGMA table_info(warehouse_documents)')}:
                c.execute('ALTER TABLE warehouse_documents ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            columns = {r[1] for r in c.execute('PRAGMA table_info(warehouse_documents)')}
            if 'deleted_at' not in columns:
                c.execute('ALTER TABLE warehouse_documents ADD COLUMN deleted_at TEXT')
            if 'archived_at' not in columns:
                c.execute('ALTER TABLE warehouse_documents ADD COLUMN archived_at TEXT')
            self.init_settings(c)
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

    def create(self, header, lines, expected):
        document_id, ts = uuid.uuid4().hex, now()
        with self.connection(True) as c:
            header = {**header, 'strict_owner': header.get('strict_owner', self._settings(c)['values']['strict_owner_default'])}
            c.execute('''INSERT INTO warehouse_documents
                (id,kind,number,date,org_inn,org_name,recipient_name,recipient_inn,
                 strict_owner,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (document_id, header['kind'], header['number'], header['date'],
                 header['org_inn'], header['org_name'], header.get('recipient_name', ''),
                 header.get('recipient_inn', ''), int(header.get('strict_owner', True)), ts, ts))
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
            self._event(c, document_id, 'created', {'number':header['number'],'kind':header['kind']}, actor='admin')
        return self.get(document_id)

    def _snapshot(self, c, document_id, include_deleted=False):
        d = self._doc(c, document_id, include_deleted)
        self._issue_counts(c, d)
        d['lines'] = [dict(r) for r in c.execute(
            'SELECT gtin,pn,pn_source,quantity FROM warehouse_lines WHERE document_id=? ORDER BY gtin', (document_id,))]
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
        d['check_current'] = d['result'] is not None and d['result_version'] == d['version']
        d['can_complete'] = (d['status'] == 'active' and not d['deleted_at'] and not d['archived_at'] and not d['checking'] and
                             d['check_current'] and d['result']['ok'])
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
                d['result'] = json.loads(d['result']) if d['result'] else None
                d['check_current'] = d['result'] is not None and d['result_version'] == d['version']
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
            d = self._doc(c, document_id)
            if d['archived_at']:
                raise WarehouseError('Сначала верните документ из архива', 409)
            if d['status'] != ('completed' if reopen else 'draft'):
                raise WarehouseError('Недопустимый переход статуса', 409)
            c.execute('''UPDATE warehouse_documents SET status='active',started_at=?,
                completed_at=NULL, version=version+1, revision=revision+1, updated_at=? WHERE id=?''',
                (now(), now(), document_id))
            self._event(c, document_id, 'reopened' if reopen else 'started', actor='admin')
        return self.get(document_id)

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
