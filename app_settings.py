"""Validated runtime policy, persisted with optimistic locking and an audit trail."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import settings

Verdict = Literal['ok', 'warning', 'error']
Status = Literal['EMITTED', 'APPLIED', 'INTRODUCED']


class ApplicationSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    report_issues: bool = Field(True, title='Сообщить ответственному', description='Склад может отправлять обращения по расхождениям.')
    return_to_warehouse: bool = Field(True, title='Возврат на склад', description='Ответственный может передавать складу комментарии по обращениям. Сканирование не блокируется.')
    worker_delete_scans: bool = Field(True, title='Удаление сканов складом', description='Сотрудник может удалять КМ из незавершённого документа.')
    manual_input: bool = Field(True, title='Ручной ввод на складе', description='Показывать кнопку экранной клавиатуры. Сканер продолжает работать.')
    auto_pn: bool = Field(True, title='Получать PN из True API', description='Автоматически дополнять PN моделью/артикулом при импорте и проверке отгрузки.')
    report_comment_required: bool = Field(False, title='Комментарий склада обязателен', description='При отправке обращения сотрудник должен описать расхождение.')
    strict_owner_default: bool = Field(True, title='Строгое соответствие организации по умолчанию', description='Только для новых отгрузок. В карточке документа правило можно изменить отдельно.')
    receipt_default_org: str = Field('', max_length=12, title='Организация приёмки по умолчанию', description='Подставляется в новый шаблон и форму импорта. Организацию в файле не заменяет.')
    shipment_statuses: list[Status] = Field(default_factory=lambda: ['INTRODUCED'], min_length=1, max_length=3, title='Допустимые статусы отгрузки', description='Для всех незавершённых отгрузок. После изменения потребуется повторная проверка.')
    check_timeout_seconds: int = Field(540, ge=30, le=540, title='Ожидание проверки, секунд', description='Максимальное время проверки документа через True API: от 30 до 540 секунд.')
    verdict_emitted: Verdict = Field('warning', title='Эмитирован (EMITTED)', description='Вердикт в одиночной и пакетной сверке КМ.')
    verdict_applied: Verdict = Field('ok', title='Нанесён (APPLIED)', description='Вердикт в одиночной и пакетной сверке КМ.')
    verdict_introduced: Verdict = Field('ok', title='В обороте (INTRODUCED)', description='Вердикт в одиночной и пакетной сверке КМ.')
    verdict_unknown: Literal['warning', 'error'] = Field('warning', title='Неизвестный статус', description='Вердикт для статуса, которого приложение ещё не знает. Ошибки API и структуры КМ остаются ошибками.')

    @field_validator('shipment_statuses')
    @classmethod
    def unique_statuses(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Статусы не должны повторяться')
        return sorted(value)


def defaults():
    return ApplicationSettings(receipt_default_org=settings.receipt_default_org).model_dump()


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0, strict=True)
    values: ApplicationSettings


class RuntimeSettings:
    def require_feature(self, c, name):
        from warehouse_store import WarehouseError
        values = self._settings(c)['values']
        if not values[name]:
            raise WarehouseError('Эта функция отключена в настройках администратором', 409)
        return values

    @staticmethod
    def init_settings(c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS application_settings (
          id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
          updated_at TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_settings_history (
          revision INTEGER PRIMARY KEY, created_at TEXT NOT NULL, changes TEXT NOT NULL);
        ''')

    @staticmethod
    def _settings(c):
        row = c.execute('SELECT * FROM application_settings WHERE id=1').fetchone()
        return {'revision': row['revision'] if row else 0,
                'updated_at': row['updated_at'] if row else None,
                'values': ApplicationSettings.model_validate({**defaults(), **(json.loads(row['body']) if row else {})}).model_dump()}

    def application_settings(self):
        with self.connection() as c:
            return self._settings(c)

    def settings_history(self):
        with self.connection() as c:
            return [dict(revision=r['revision'], created_at=r['created_at'], changes=json.loads(r['changes']))
                    for r in c.execute('SELECT * FROM application_settings_history ORDER BY revision DESC LIMIT 50')]

    def update_settings(self, revision, values):
        from organizations import registry
        from warehouse_store import WarehouseError, now

        values = ApplicationSettings.model_validate(values).model_dump()
        if values['receipt_default_org'] and not registry.has(values['receipt_default_org']):
            raise WarehouseError('Организация приёмки отсутствует в справочнике')
        with self.connection(True) as c:
            previous = self._settings(c)
            if previous['revision'] != revision:
                raise WarehouseError('Настройки изменены другим администратором. Обновите настройки и повторите изменения.', 409)
            changes = {k: {'before': previous['values'][k], 'after': v} for k, v in values.items() if previous['values'][k] != v}
            if changes:
                ts, revision = now(), revision + 1
                c.execute('INSERT OR REPLACE INTO application_settings VALUES (1,?,?,?)',
                          (revision, ts, json.dumps(values, ensure_ascii=False)))
                c.execute('INSERT INTO application_settings_history VALUES (?,?,?)',
                          (revision, ts, json.dumps(changes, ensure_ascii=False)))
                if 'shipment_statuses' in changes:
                    ids = [r['id'] for r in c.execute("SELECT id FROM warehouse_documents WHERE kind='shipment' AND status!='completed'")]
                    for document_id in ids:
                        self._bump(c, document_id)
                        self._event(c, document_id, 'policy_changed', {'settings_revision': revision, 'changes': changes['shipment_statuses']}, actor='admin')
        return self.application_settings()
