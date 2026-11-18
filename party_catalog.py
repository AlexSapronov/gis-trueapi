"""Persistent legal entity names and their INNs for invoice imports."""
import unicodedata


def name_key(value):
    if not isinstance(value, str):
        raise ValueError('Наименование юрлица должно быть текстом')
    value = unicodedata.normalize('NFKC', value)
    value = value.translate(str.maketrans({'«': '"', '»': '"', '“': '"', '”': '"', '„': '"'}))
    key = ' '.join(value.split()).casefold()
    if not key or len(key) > 1000:
        raise ValueError('Заполните наименование юрлица (до 1000 символов)')
    return key


def inn_text(value):
    # Client tax identifiers are opaque: foreign identifiers need not use Russian INN length.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('Заполните ИНН / налоговый идентификатор')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError('ИНН должен быть целым числом')
        value = str(int(value))
    inn = str(value or '').strip()
    if not inn:
        raise ValueError('Заполните ИНН / налоговый идентификатор')
    return inn


class PartyCatalog:
    @staticmethod
    def init_party_catalog(c):
        c.execute('''CREATE TABLE IF NOT EXISTS warehouse_party_catalog (
            name_key TEXT NOT NULL, inn TEXT NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY (name_key, inn))''')
        if any(row[1] == 'inn' and not row[5] for row in c.execute(
                'PRAGMA table_info(warehouse_party_catalog)')):
            c.execute('ALTER TABLE warehouse_party_catalog RENAME TO warehouse_party_catalog_legacy')
            c.execute('''CREATE TABLE warehouse_party_catalog (
                name_key TEXT NOT NULL, inn TEXT NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY (name_key, inn))''')
            c.execute('INSERT INTO warehouse_party_catalog SELECT * FROM warehouse_party_catalog_legacy')
            c.execute('DROP TABLE warehouse_party_catalog_legacy')

    def party_catalog(self):
        with self.connection() as c:
            return {'rows': [dict(r) for r in c.execute(
                'SELECT inn,name FROM warehouse_party_catalog ORDER BY name_key,inn')]}

    @staticmethod
    def _party_inns(c, key):
        return [r['inn'] for r in c.execute(
            'SELECT inn FROM warehouse_party_catalog WHERE name_key=? ORDER BY inn', (key,))]

    def clear_party_catalog(self):
        with self.connection(True) as c:
            return {'deleted': c.execute('DELETE FROM warehouse_party_catalog').rowcount}

    def save_party_catalog(self, rows, *, replace=False):
        from warehouse_store import WarehouseError
        # Validate the whole batch before an optional replacement.
        entries = {}
        for row in rows:
            try:
                key, inn = name_key(row['name']), inn_text(row['inn'])
            except ValueError as exc:
                raise WarehouseError(str(exc)) from exc
            entries[key, inn] = row['name'].strip()
        if not entries:
            raise WarehouseError('В справочнике нет заполненных строк')
        with self.connection(True) as c:
            if replace:
                c.execute('DELETE FROM warehouse_party_catalog')
            before = {(r['name_key'], r['inn']) for r in c.execute('SELECT name_key,inn FROM warehouse_party_catalog')}
            c.executemany('''INSERT INTO warehouse_party_catalog VALUES (?,?,?)
                ON CONFLICT(name_key,inn) DO UPDATE SET name=excluded.name''',
                ((key, inn, name) for (key, inn), name in entries.items()))
            return {'added': len(entries.keys() - before),
                    'existing': len(entries.keys() & before),
                    'total': c.execute('SELECT count(*) FROM warehouse_party_catalog').fetchone()[0]}
