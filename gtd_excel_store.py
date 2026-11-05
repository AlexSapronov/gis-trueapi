"""Administrator-only durable Excel correction jobs."""
import json
import uuid


class GtdExcelStore:
    @staticmethod
    def init_gtd_excel(c):
        c.execute('''CREATE TABLE IF NOT EXISTS gtd_excel_jobs (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, source BLOB NOT NULL,
            status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '', report TEXT, versions TEXT, output BLOB,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')

        c.execute('CREATE TABLE IF NOT EXISTS gtd_good_gtin (good_id TEXT PRIMARY KEY,gtin TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS gtd_catalog_meta (id INTEGER PRIMARY KEY,revision INTEGER NOT NULL)')
        c.execute('INSERT OR IGNORE INTO gtd_catalog_meta VALUES (1,0)')

    def gtd_catalog(self):
        with self.connection() as c:
            revision=c.execute('SELECT revision FROM gtd_catalog_meta WHERE id=1').fetchone()[0]
            rows=[dict(row) for row in c.execute('SELECT good_id,gtin FROM gtd_good_gtin ORDER BY good_id')]
            return dict(revision=revision,rows=rows)

    def gtd_catalog_save(self, mapping):
        with self.connection(True) as c:
            before=dict(c.execute('SELECT good_id,gtin FROM gtd_good_gtin'))
            changes=[dict(good_id=key,before=before.get(key,''),after=value) for key,value in mapping.items() if before.get(key)!=value]
            for item in changes:
                c.execute('INSERT INTO gtd_good_gtin VALUES (?,?) ON CONFLICT(good_id) DO UPDATE SET gtin=excluded.gtin',
                          (item['good_id'],item['after']))
            if changes:c.execute('UPDATE gtd_catalog_meta SET revision=revision+1 WHERE id=1')
            return dict(added=sum(not item['before'] for item in changes),
                        updated=sum(bool(item['before']) for item in changes),unchanged=len(mapping)-len(changes),changes=changes)

    def gtd_excel_create(self, filename, source):
        from warehouse_store import WarehouseError, now
        with self.connection(True) as c:
            if c.execute("SELECT 1 FROM gtd_excel_jobs WHERE status IN ('queued','running')").fetchone():
                raise WarehouseError('Дождитесь завершения текущей проверки Excel', 409)
            job_id=uuid.uuid4().hex
            c.execute('INSERT INTO gtd_excel_jobs (id,filename,source,status,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                      (job_id,filename,source,'queued',now(),now()))
        return job_id

    def gtd_excel_get(self, job_id, files=False):
        from warehouse_store import WarehouseError
        columns='*' if files else 'id,filename,status,progress,total,message,report,created_at,updated_at'
        with self.connection() as c:
            row=c.execute('SELECT '+columns+' FROM gtd_excel_jobs WHERE id=?',(job_id,)).fetchone()
            if row is None:raise WarehouseError('Проверка Excel не найдена',404)
            result=dict(row)
            result['report']=json.loads(result['report']) if result['report'] else None
            if files:result['versions']=json.loads(result['versions']) if result['versions'] else {}
            return result

    def gtd_excel_progress(self, job_id, progress, total):
        from warehouse_store import now
        with self.connection(True) as c:
            c.execute("UPDATE gtd_excel_jobs SET status='running',progress=?,total=?,updated_at=? WHERE id=?",
                      (progress,total,now(),job_id))

    def _gtd_excel_versions(self, c, versions):
        from warehouse_store import WarehouseError
        catalog_revision=versions.get('_catalog_revision')
        if catalog_revision is not None and c.execute('SELECT revision FROM gtd_catalog_meta WHERE id=1').fetchone()[0]!=catalog_revision:
            raise WarehouseError('Справочник GoodID → GTIN изменился. Проверьте Excel повторно.',409)
        for document_id, expected in versions.items():
            if document_id=='_catalog_revision':continue
            d=self._snapshot(c,document_id)
            if ([d['version'],d['revision']]!=expected or d['status']!='completed'
                    or not d['check_current'] or not (d['result'] or {}).get('ok')):
                raise WarehouseError('Связанная отгрузка изменилась. Проверьте Excel повторно.',409)

    def gtd_excel_finish(self, job_id, output, report, versions):
        from warehouse_store import now
        with self.connection(True) as c:
            self._gtd_excel_versions(c,versions)
            c.execute("UPDATE gtd_excel_jobs SET status='completed',output=?,report=?,versions=?,updated_at=? WHERE id=?",
                      (output,json.dumps(report,ensure_ascii=False),json.dumps(versions),now(),job_id))

    def gtd_excel_download(self, job_id):
        from warehouse_store import WarehouseError
        job=self.gtd_excel_get(job_id,files=True)
        if job['status']!='completed' or job['output'] is None:
            raise WarehouseError('Результат ещё не готов',409)
        with self.connection() as c:self._gtd_excel_versions(c,job['versions'])
        return job

    def gtd_excel_fail(self, job_id, message):
        from warehouse_store import now
        with self.connection(True) as c:
            c.execute("UPDATE gtd_excel_jobs SET status='failed',message=?,updated_at=? WHERE id=?",
                      (message,now(),job_id))

    def gtd_excel_recover(self):
        with self.connection(True) as c:
            c.execute("UPDATE gtd_excel_jobs SET status='failed',message='Приложение перезапущено. Загрузите файл повторно.' WHERE status IN ('queued','running')")
