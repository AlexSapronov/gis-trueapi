# Установка, доступ и эксплуатация

Редакция: 16.11.2026. [Навигация](../README.md).

## Архитектура

Проект сознательно простой: FastAPI, Uvicorn, httpx, Pydantic, Jinja2,
vanilla JS/CSS, SQLite для документов и файловое хранилище токенов. Без React, Redis, Celery,
микросервисов и внешней БД.

Браузер обращается к FastAPI; сервис проверки вызывает True API. Складские
документы, проверки, XML и результаты Excel сохраняются в локальной SQLite.
Клиентские токены ГИС МТ хранятся отдельно на backend.

`TokenStore` (`tokens.py`) — bearer-токены по организациям (файловое
хранилище, заменяемое через `TokenStore` Protocol).

---

## Структура проекта

```text
main.py                     FastAPI-приложение, REST API, /admin/token
service.py                  бизнес-логика: scan / batch / balance / exchange
trueapi.py                  MarkingApiClient Protocol + live-клиент True API
mockclient.py               mock-реализация клиента (детерминированная)
models.py                   модели, маппинг статусов, verdict policy, ошибки
datamatrix.py               парсер Data Matrix (GS1, FNC1, checksum GTIN)
tokens.py                   файловое хранилище bearer-токенов по организациям
organizations.py            справочник организаций
config.py                   конфигурация (.env), разбор организаций
buildinfo.py                build_id (git SHA / source hash)
schemas.py                  Pydantic-схемы REST
static/                     app.js, style.css
templates/                  сверка, склад, кабинет отдела продаж
tests/                      pytest
refresh_trueapi_token.ps1   PowerShell: UUID+SIGNATURE (УКЭП через КриптоПро)
```

---

## Конфигурация

Все переменные читаются через `config.py` (`.env` / env):

| Переменная       | Назначение                                  | По умолчанию                |
| ---------------- | ------------------------------------------- | --------------------------- |
| `GIS_MODE`       | `mock` или `live`                           | `mock`                      |
| `GIS_BASE_URL`   | базовый URL True API                        | `https://markirovka.crpt.ru` |
| `ADMIN_KEY`      | ключ доступа к `/admin/*`                   | `change-me-in-production`   |
| `PRODUCT_GROUPS` | товарные группы для `cises/search`          | `radio`                     |
| `ORGANIZATIONS`  | список `INN=Название;...`                   | 4 вымышленные организации (см. `config.py`) |
| `TOKENS_FILE`    | путь к файлу токенов                        | `data/tokens.json`          |
| `WAREHOUSE_DB` | SQLite: документы, КМ, проверки, XML, результаты Excel | `data/warehouse.sqlite3` |
| `RECEIPT_DEFAULT_ORG` | ИНН приёмки до сохранения настроек | пусто |
| `SALES_SHIPMENT_LINKS` | JSON ключей персональных ссылок ОП | `{}` |
| `RECEIPT_BRIDGE_KEY` | Отдельный ключ интеграции приёмок | пусто (отключено) |
| `BUILD_ID`       | явный build_id (опционально)                | не задан                    |

Значения ADMIN_KEY и токенов задаются при установке и не публикуются.
`ADMIN_KEY` по умолчанию `change-me-in-production` — в таком виде доступ к
`/admin/*` запрещён (fail-closed).

---

## Авторизация ГИС МТ

Обновление bearer-токена организации — через служебную страницу `/admin/token`
(ссылки на неё в основном интерфейсе нет; UUID-токен живёт ~10 часов по данным ГИС МТ).

Auth-flow (по актуальной документации ЦРПТ «Описание True API»):

Сервер получает `uuid` и `data` через `GET /auth/key`. Локальный КриптоПро
подписывает challenge, затем backend отправляет `POST /auth/simpleSignIn`
с `unitedToken: true` и сохраняет `uuidToken` для `Authorization: Bearer`.

Порядок:

1. На Windows-машине с КриптоПро CSP и действующей УКЭП запустить
   `refresh_trueapi_token.ps1` — либо двойным кликом по удобному лаунчеру
   `Обновить токен ЧЗ.cmd` (лежит в той же директории и вызывает PowerShell-скрипт):

   ```powershell
   .\refresh_trueapi_token.ps1
   ```

2. Скрипт автоматически ищет доступные сейчас контейнеры закрытых ключей
   КриптоПро (`csptest -keyset -enum_cont -verifycontext -fqcn`), сопоставляет
   их с действующими сертификатами УКЭП в личном хранилище и показывает
   пользователю список. Вы выбираете нужную ЭЦП, скрипт получает challenge
   (`GET /auth/key`) и подписывает `data` УКЭП через `csptest.exe` — подпись
   выполняется по отпечатку (thumbprint), а не по ФИО. На выходе печатаются
   `UUID` + `SIGNATURE`.
3. Открыть `/admin`, войти и выбрать организацию в разделе «Токен».
4. Вставить `UUID` в первое поле, `SIGNATURE` — во второе, нажать «Обновить токен».
5. Backend обменивает `UUID` + `SIGNATURE` на единый токен аутентификации в формате
   UUID (`POST /auth/simpleSignIn` с `unitedToken: true`) и начинает использовать его
   как `Authorization: Bearer <uuidToken>` без перезапуска. ИНН выбранной организации
   (`inn`) передаётся в `/auth/simpleSignIn` вместе с UUID-flow — ЦРПТ требует ИНН
   для реальной авторизации по полномочиям пользователя/сертификата в ГИС МТ.

Bearer-токен хранится **только** на backend; frontend его не получает.
Пара `UUID` + `SIGNATURE` одноразовая и живёт считанные минуты — вставлять
свежий вывод скрипта сразу после запуска. Вручную редактировать
`$certName` больше не нужно: скрипт сам находит доступные контейнеры
КриптоПро и определяет сертификаты, а подпись выполняет по thumbprint.
Реальные auth-данные в репозиторий не попадают.

---

## Формат хранилища токенов

`data/tokens.json` — runtime secret, в `.gitignore`, не коммитится.

Поддерживаются два формата (оба читаются, запись всегда в новом):

Legacy:

```json
{
  "0000000001": "TOKEN"
}
```

Новый (с метаданными):

```json
{
  "0000000001": {
    "token": "...",
    "updated_at": "2026-09-11T05:30:00Z"
  }
}
```

Старые записи читаются как есть (`updated_at = null`). При следующем
обновлении токена запись мигрирует в новый формат. Права файла — `0600`.

---

## Основные API

- `GET /` — интерфейс проверки КМ (рендер `templates/index.html`).
- `GET /api/status` — состояние backend, True API, организаций, `build_id`.
- `POST /api/scan` — проверка одного КМ.
- `POST /api/scan_batch` — проверка списка КМ (до 1000).
- `POST /api/balance` — баланс по GTIN (статусы EMITTED/APPLIED/INTRODUCED).
- `GET /admin` — общая админка с единым входом.
- `GET /admin/token` — перенаправление в раздел «Токен» общей админки.
- `POST /admin/token` — обмен UUID+SIGNATURE на bearer-токен (защищён).

---

## Mock-режим

Детерминированный mock без доступа к ЧЗ (`GIS_MODE=mock`). Тестовый GTIN,
который считается относящимся к выбранной организации, задан в `mockclient.py`; для локальной проверки
используйте сценарии `tests/`, а не идентификаторы реального товара.

Маркеры в serial:

- `Q<число>` → `quantityInPack = <число>` (например `Q500` → 500);
- `EMITTED` → статус `EMITTED`;
- `INTRODUCED` → статус `INTRODUCED`;
- `NOTFOUND` → «КМ не найден»;
- иначе → статус `APPLIED`, quantity `400`.

Чужой GTIN → «чужой» КМ.

---

## Тестирование

```bash
python -m pytest -q
node --check static/app.js
```

CI (`.github/workflows/ci.yml`) на push/PR в `main` запускает pytest
(mock-режим, Python 3.11 и 3.12) и проверку синтаксиса JS.

Результат проверки относится к конкретному коммиту; актуальный статус смотрите в CI.

---

## Развёртывание

Пример установки: `/opt/gis-trueapi`, systemd-юнит `gis-trueapi.service`.
Настройте `WorkingDirectory`, `ExecStart` виртуального окружения и `EnvironmentFile`
на своей системе. Для reverse proxy привяжите Uvicorn к `127.0.0.1:8000`.

```bash
cd /opt/gis-trueapi
git pull --ff-only
.venv/bin/python -m pip install -r requirements-lock.txt
sudo systemctl restart gis-trueapi.service
sudo systemctl status gis-trueapi.service
```

Файлы `.env` и `data/tokens.json` живут только на сервере (в `.gitignore`).

---

## Защита доступа

- bearer-токены хранятся только на backend, frontend их не получает и не
  логирует;
- `data/tokens.json` в `.gitignore`, права файла `0600`;
- `/admin/*` защищён `ADMIN_KEY` (`hmac.compare_digest`, fail-closed);
- не логировать token / signature / ADMIN_KEY;
- приватные ключи УКЭП на сервере не хранятся (КриптоПро остаётся на Windows-ПК);
- в production — HTTPS через reverse proxy (Caddy/nginx), не выставлять Uvicorn
  голым портом.

## Резервные копии и восстановление

Используйте SQLite Backup API для `WAREHOUSE_DB`, отдельно сохраняйте `.env` и
`TOKENS_FILE` с ограниченными правами. В базе находятся также исходные/готовые XML,
связи PN, справочники, результаты Excel и история обращений. Рабочие данные
не включаются в публичную документацию. Порядок копирования и восстановления
SQLite приведён в [руководстве склада](WAREHOUSE.md#хранение-и-резервное-копирование).

## Дополнительные модули

`warehouse_store.py`, `warehouse_service.py`, `warehouse_routes.py` обслуживают
складские документы. `upd_xml.py` и `upd_store.py` готовят и сохраняют УПД;
`gtd_excel.py`, `gtd_excel_store.py`, `gtd_excel_routes.py` — сводные проверки;
`km_report.py` — отчёты по списку КМ; `warehouse_labels.py` — недостающие PDF;
`shipment_bulk.py` — массовый импорт заданий на отгрузку;
`receipt_bridge.py` — приёмки из marking-bm; `sales_shipments.py` — кабинет ОП.
