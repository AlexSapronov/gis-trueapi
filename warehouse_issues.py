"""Problem reports retain the exact failed check and document-local diagnostics."""
from __future__ import annotations

import json
import uuid


class WarehouseIssues:
    @staticmethod
    def _event(c, document_id, action, details=None, actor='warehouse'):
        from warehouse_store import now
        c.execute('INSERT INTO warehouse_events(document_id,created_at,actor,action,details) VALUES(?,?,?,?,?)',
                  (document_id, now(), actor, action, json.dumps(details or {}, ensure_ascii=False)))

    @staticmethod
    def _issue_counts(c, d):
        row = c.execute('''SELECT count(*) AS total, sum(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS opened
            FROM warehouse_issues WHERE document_id=?''', (d['id'],)).fetchone()
        d['issue_count'], d['open_issues'] = row['total'], row['opened'] or 0
        d['reported_current_result'] = bool(c.execute('SELECT 1 FROM warehouse_issues WHERE document_id=? AND result_at=? AND resolved_at IS NULL', (d['id'],d['result_at'])).fetchone())
        d['warehouse_replies'] = [dict(r) for r in c.execute('''SELECT id,issue_id,message,created_at
            FROM warehouse_returns WHERE document_id=? AND acknowledged_at IS NULL ORDER BY created_at,id''',(d['id'],))]
        d['pending_returns'] = len(d['warehouse_replies'])

    def restore(self, document_id, version, revision):
        from warehouse_store import WarehouseError
        with self.connection(True) as c:
            d = self._doc(c, document_id, include_deleted=True)
            if not d['deleted_at']:
                raise WarehouseError('Документ не находится в корзине', 409)
            if d['version'] != version or d['revision'] != revision:
                raise WarehouseError('Документ изменён. Обновите карточку.', 409)
            c.execute('UPDATE warehouse_documents SET deleted_at=NULL WHERE id=?', (document_id,))
            self._bump(c, document_id)
            if d['status']=='completed' and d['result'] and json.loads(d['result']).get('ok'):
                c.execute('UPDATE warehouse_documents SET result_version=version WHERE id=?',(document_id,))
            self._event(c, document_id, 'restored', actor='admin')
        return self.get(document_id)

    def report_issue(self, document_id, version, result_at, message=''):
        from warehouse_store import WarehouseError, now
        with self.connection(True) as c:
            d = self._snapshot(c, document_id)
            self._active(d)
            if d['version'] != version or d['result_at'] != result_at or not d['check_current']:
                raise WarehouseError('Состав или результат проверки изменён. Выполните актуальную проверку.', 409)
            if d['checking'] or d['result']['ok']:
                raise WarehouseError('Сообщение доступно после проверки с расхождениями', 409)
            existing = c.execute('''SELECT id FROM warehouse_issues WHERE document_id=? AND result_at=?
                AND resolved_at IS NULL''', (document_id, result_at)).fetchone()
            if existing:
                return {'id': existing['id'], 'duplicate': True}
            issue_id, ts = uuid.uuid4().hex, now()
            c.execute('''INSERT INTO warehouse_issues
                (id,document_id,version,result_at,reported_at,message,snapshot) VALUES(?,?,?,?,?,?,?)''',
                (issue_id, document_id, version, result_at, ts, message.strip(), json.dumps(d, ensure_ascii=False)))
            c.execute('UPDATE warehouse_documents SET revision=revision+1,updated_at=? WHERE id=?', (ts, document_id))
            self._event(c, document_id, 'issue_reported', {'issue_id':issue_id,'message':message.strip()})
            return {'id':issue_id,'duplicate':False}

    def issues(self, document_id):
        with self.connection() as c:
            self._doc(c, document_id, include_deleted=True)
            return [dict(r) for r in c.execute('''SELECT id,document_id,version,result_at,reported_at,message,
                resolved_at,resolution FROM warehouse_issues WHERE document_id=? ORDER BY reported_at DESC,id''', (document_id,))]

    def issue(self, document_id, issue_id):
        from warehouse_store import WarehouseError
        with self.connection() as c:
            row = c.execute('SELECT * FROM warehouse_issues WHERE document_id=? AND id=?', (document_id, issue_id)).fetchone()
            if not row:
                raise WarehouseError('Обращение не найдено', 404)
            d = dict(row)
            d['snapshot'] = json.loads(d['snapshot'])
            d['returns'] = [dict(r) for r in c.execute('''SELECT id,message,created_at,acknowledged_at
                FROM warehouse_returns WHERE document_id=? AND issue_id=? ORDER BY created_at DESC,id''',(document_id,issue_id))]
            return d

    def issue_events(self, document_id, issue_id, before=None, limit=100):
        from warehouse_store import WarehouseError
        with self.connection() as c:
            if not c.execute('SELECT 1 FROM warehouse_issues WHERE id=? AND document_id=?', (issue_id,document_id)).fetchone():
                raise WarehouseError('Обращение не найдено',404)
            rows = c.execute('''SELECT * FROM warehouse_events WHERE document_id=? AND (? IS NULL OR id<?)
                ORDER BY id DESC LIMIT ?''', (document_id,before,before,limit+1)).fetchall()
            events = [{**dict(r),'details':json.loads(r['details'])} for r in rows[:limit]]
            return {'events':events,'next_before':events[-1]['id'] if len(rows)>limit else None}

    def resolve_issue(self, document_id, issue_id, resolution):
        from warehouse_store import WarehouseError, now
        with self.connection(True) as c:
            row = c.execute('SELECT * FROM warehouse_issues WHERE id=? AND document_id=?',(issue_id,document_id)).fetchone()
            if not row:
                raise WarehouseError('Обращение не найдено',404)
            if row['resolved_at']:
                return {'resolved':True}
            if not resolution.strip():
                raise WarehouseError('Укажите результат разбора обращения')
            ts=now()
            c.execute('UPDATE warehouse_issues SET resolved_at=?,resolution=? WHERE id=?',(ts,resolution.strip(),issue_id))
            c.execute('UPDATE warehouse_documents SET revision=revision+1,updated_at=? WHERE id=?',(ts,document_id))
            self._event(c,document_id,'issue_resolved',{'issue_id':issue_id,'resolution':resolution.strip()},actor='admin')
            return {'resolved':True}

    def return_to_warehouse(self, document_id, issue_id, message, request_id):
        """Send a durable reply; never lock scanning, change composition, or reopen a document."""
        from warehouse_store import WarehouseError, now
        message=message.strip()
        if not message:
            raise WarehouseError('Напишите складу, что было исправлено и что нужно сделать')
        with self.connection(True) as c:
            d=self._doc(c,document_id)
            self._active(d)
            issue=c.execute('SELECT * FROM warehouse_issues WHERE document_id=? AND id=?',(document_id,issue_id)).fetchone()
            if not issue:
                raise WarehouseError('Обращение не найдено',404)
            previous=c.execute('SELECT * FROM warehouse_returns WHERE document_id=? AND request_id=?',(document_id,request_id)).fetchone()
            if previous:
                if previous['issue_id'] != issue_id or previous['message'] != message:
                    raise WarehouseError('Этот запрос уже использован для другого ответа',409)
                return {'id':previous['id'],'duplicate':True}
            reply_id,ts=uuid.uuid4().hex,now()
            c.execute('INSERT INTO warehouse_returns(id,document_id,issue_id,request_id,message,created_at) VALUES(?,?,?,?,?,?)',
                      (reply_id,document_id,issue_id,request_id,message,ts))
            if not issue['resolved_at']:
                c.execute('UPDATE warehouse_issues SET resolved_at=?,resolution=? WHERE id=?',(ts,message,issue_id))
                self._event(c,document_id,'issue_resolved',{'issue_id':issue_id,'resolution':message},actor='admin')
            c.execute('UPDATE warehouse_documents SET revision=revision+1,updated_at=? WHERE id=?',(ts,document_id))
            self._event(c,document_id,'returned_to_warehouse',{'issue_id':issue_id,'reply_id':reply_id,'message':message},actor='admin')
            return {'id':reply_id,'duplicate':False}

    def acknowledge_return(self, document_id, reply_id):
        from warehouse_store import WarehouseError, now
        with self.connection(True) as c:
            self._doc(c,document_id)
            row=c.execute('SELECT * FROM warehouse_returns WHERE document_id=? AND id=?',(document_id,reply_id)).fetchone()
            if not row:
                raise WarehouseError('Ответ не найден',404)
            if row['acknowledged_at']:
                return {'acknowledged':True}
            ts=now()
            c.execute('UPDATE warehouse_returns SET acknowledged_at=? WHERE id=?',(ts,reply_id))
            c.execute('UPDATE warehouse_documents SET revision=revision+1,updated_at=? WHERE id=?',(ts,document_id))
            self._event(c,document_id,'warehouse_return_acknowledged',{'reply_id':reply_id})
            return {'acknowledged':True}
