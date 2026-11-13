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
            name_key TEXT PRIMARY KEY, inn TEXT NOT NULL, name TEXT NOT NULL)''')

    def party_catalog(self):
        with self.connection() as c:
            return {'rows': [dict(r) for r in c.execute(
                'SELECT inn,name FROM warehouse_party_catalog ORDER BY name_key')]}

    def save_party_catalog(self, rows):
        from warehouse_store import WarehouseError
        # Validate the whole batch before saving; a name must never pick an arbitrary INN.
        entries = {}
        for row in rows:
            try:
                key, inn = name_key(row['name']), inn_text(row['inn'])
            except ValueError as exc:
                raise WarehouseError(str(exc)) from exc
            if key in entries and entries[key]['inn'] != inn:
                raise WarehouseError(f'Неоднозначное имя «{row["name"]}»: несколько ИНН')
            entries[key] = {'inn': inn, 'name': row['name'].strip()}
        if not entries:
            raise WarehouseError('В справочнике нет заполненных строк')
        with self.connection(True) as c:
            before = {r['name_key']: dict(r) for r in c.execute('SELECT * FROM warehouse_party_catalog')}
            for key, row in entries.items():
                if key in before and before[key]['inn'] != row['inn']:
                    raise WarehouseError(f'Имя «{row["name"]}» уже связано с ИНН {before[key]["inn"]}; другой ИНН не сохранён')
            for key, row in entries.items():
                c.execute('''INSERT INTO warehouse_party_catalog VALUES (?,?,?)
                    ON CONFLICT(name_key) DO UPDATE SET name=excluded.name''', (key, row['inn'], row['name']))
            return {'added': sum(key not in before for key in entries),
                    'existing': sum(key in before for key in entries),
                    'total': c.execute('SELECT count(*) FROM warehouse_party_catalog').fetchone()[0]}
