"""Persistent XML uploads and organization-scoped PN aliases."""
import json
import uuid


class UpdStore:
    @staticmethod
    def init_upd(c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS upd_pn_aliases (
          org_inn TEXT NOT NULL, xml_pn TEXT NOT NULL, scan_pn TEXT NOT NULL,
          PRIMARY KEY(org_inn, xml_pn));
        CREATE TABLE IF NOT EXISTS upd_uploads (
          id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES warehouse_documents(id),
          version INTEGER NOT NULL, revision INTEGER NOT NULL, source BLOB NOT NULL,
          output BLOB, filename TEXT, summary TEXT);
        CREATE INDEX IF NOT EXISTS upd_upload_document ON upd_uploads(document_id);
        ''')

    def upd_aliases(self, org_inn):
        with self.connection() as c:
            return dict(c.execute('SELECT xml_pn,scan_pn FROM upd_pn_aliases WHERE org_inn=?', (org_inn,)))

    def upd_create(self, doc, content):
        from warehouse_store import WarehouseError
        upload_id = uuid.uuid4().hex
        with self.connection(True) as c:
            current = self._doc(c, doc['id'])
            if (current['version'], current['revision']) != (doc['version'], doc['revision']):
                raise WarehouseError('Отгрузка изменилась. Загрузите XML заново.', 409)
            c.execute('INSERT INTO upd_uploads (id,document_id,version,revision,source) VALUES (?,?,?,?,?)',
                      (upload_id, doc['id'], doc['version'], doc['revision'], content))
            self._event(c, doc['id'], 'upd_uploaded', {'upload_id': upload_id}, actor='admin')
        return upload_id

    def upd_get(self, document_id, upload_id):
        from warehouse_store import WarehouseError
        with self.connection() as c:
            row = c.execute('SELECT * FROM upd_uploads WHERE id=? AND document_id=?',
                            (upload_id, document_id)).fetchone()
            if not row:
                raise WarehouseError('XML не найден', 404)
            doc = self._snapshot(c, document_id)
            if (row['version'], row['revision']) != (doc['version'], doc['revision']):
                raise WarehouseError('Отгрузка изменилась. Загрузите XML заново.', 409)
            return dict(row), doc

    def upd_finish(self, document_id, upload_id, output, filename, summary, aliases):
        from warehouse_store import WarehouseError
        with self.connection(True) as c:
            row = c.execute('SELECT * FROM upd_uploads WHERE id=? AND document_id=?',
                            (upload_id, document_id)).fetchone()
            doc = self._doc(c, document_id)
            if not row or (row['version'], row['revision']) != (doc['version'], doc['revision']):
                raise WarehouseError('Отгрузка изменилась во время подготовки XML. Загрузите XML заново.', 409)
            c.execute('UPDATE upd_uploads SET output=?,filename=?,summary=? WHERE id=?',
                      (output, filename, json.dumps(summary, ensure_ascii=False), upload_id))
            for xml_pn, scan_pn in aliases.items():
                c.execute('INSERT INTO upd_pn_aliases VALUES (?,?,?) ON CONFLICT(org_inn,xml_pn) '
                          'DO UPDATE SET scan_pn=excluded.scan_pn', (doc['org_inn'], xml_pn, scan_pn))
            self._event(c, document_id, 'upd_prepared' if output is not None else 'upd_review_required',
                        {'upload_id': upload_id, 'rows': summary, 'saved_aliases': aliases}, actor='admin')
