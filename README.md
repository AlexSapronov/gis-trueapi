# GIS MT True API — внутренняя проверка кодов маркировки «Честный Знак»

Внутреннее web-приложение для склада: проверка кодов маркировки (КМ) через
True API ГИС МТ. Основной сценарий — Android-ТСД с аппаратным DataMatrix-сканером
под Fully Kiosk Browser (сканер в режиме `output to cursor`).

Назначение:

- сканирование КМ и проверка фактического состояния через True API;
- понятный «складской» вердикт по каждому коду (OK / внимание / ошибка);
- пакетная проверка списка КМ;
- баланс по GTIN;
- работа нескольких юридических лиц, каждое со своим bearer-токеном.

Приложение сознательно простое: FastAPI + vanilla JS, без внешней БД,
очередей и микросервисов.

---

## Возможности

### Одиночный скан

Один DataMatrix (КМ), запрос свежих данных True API. Результаты проверки
**не кэшируются** — каждый скан даёт актуальный ответ ЧЗ.

Карточка результата показывает:

- `productName`;
- `quantityInPack`;
- status (с русским маппингом);
- owner (наш / чужой);
- GTIN и Serial (в раскрываемых технических данных).

### Business policy (verdict)

Интерпретация фактического статуса ЧЗ для сотрудника склада. Реализована
на backend в `models.py` (`business_verdict`) — явный whitelist, без принципа
«нет `error_category` => OK»:

| True API status | verdict | UI      |
| --------------- | ------- | ------- |
| `APPLIED`       | ok      | зелёный |
| `INTRODUCED`    | ok      | зелёный |
| `EMITTED`       | warning | жёлтый  |
| неизвестный     | warning | жёлтый  |
| ошибка DM/API/КМ| error   | красный |

`EMITTED` = код эмитирован, но нанесение ещё не зарегистрировано. Это НЕ
красная ошибка и НЕ зелёный успех — для склада это жёлтое предупреждение:
если код физически уже нанесён на товар, следует проверить отчёт о нанесении.

Неизвестный (новый) статус True API по умолчанию **не** считается зелёным —
отображается как warning, без придумывания бизнес-смысла.

Разделение ответственности:

- `TrueApiClient` возвращает факты (сырой статус ЧЗ);
- `Service` / business policy определяет verdict;
- frontend только отображает готовый verdict.

### quantityInPack

`quantityInPack` — количество единиц товара, зашитое в конкретный КМ при
отчёте о нанесении. Это **не** количество кодов и **не** balance quantity.

В UI одно из главных полей, порядок:
`productName -> quantityInPack -> status -> owner`.

### Пакетная проверка (batch)

Несколько КМ (до 1000 на запрос). Сводка по результату: Total, OK, Warning,
Error. По каждой строке — свой verdict и `quantityInPack`, раскрываемые
технические данные.

### Баланс

Баланс по GTIN и статусам `EMITTED` / `APPLIED` / `INTRODUCED`. Каждый статус
считается независимо. Разделяются два показателя:

- `km_count` — количество КМ;
- `quantity_sum` — сумма `quantityInPack` по КМ (заполняется для `APPLIED` и
  `INTRODUCED`; для `EMITTED` поле не заполняется).

Итоги считаются только по организациям **с токеном**; организации без токена
перечисляются на уровне организации, но не тянут вниз общий итог.

### Несколько организаций

Справочник организаций (`organizations.py` + конфиг `ORGANIZATIONS`) и
отдельный bearer-токен на каждую (`tokens.py`). Выбор организации для запроса
через `org_inn` или автоматически — первая организация с настроенным токеном.

---

## Kiosk / ТСД UX

Приложение рассчитано на Android-ТСД. Сканер работает в режиме
`output to cursor` (аппаратный сканер печатает символы в текущий focus).

### Scan focus lock

Пока открыта вкладка «Скан», поле сканирования автоматически держит focus —
сотруднику не нужно вручную тапать по input перед каждым КМ:

- focus восстанавливается после `blur`, клика/tap, возврата приложения
  (`window focus`, `pageshow`, `visibilitychange`);
- глобальный keyboard-wedge fallback ловит скан, если WebView всё же потерял
  focus (printable-символы идут в поле, `Enter` запускает проверку);
- восстановление event-driven, без агрессивного polling;
- вкладки Batch и Balance **не** перехватываются, ввод не воруется из
  `INPUT`/`TEXTAREA`/contenteditable.

Атрибут `inputmode="none"` используется, чтобы не поднимать Android soft
keyboard при автоматическом фокусе.

---

## Verdict policy

Таблица — см. раздел «Business policy (verdict)» выше.

Ключевой принцип: `EMITTED` и неизвестные статусы **не** меняют глобальное
состояние True API. Если True API успешно ответил — верхняя системная панель
остаётся зелёной («СИСТЕМА РАБОТАЕТ»); жёлтым становится только результат
конкретного КМ.

---

## True API health / status bar

Status bar отслеживает состояние системы и True API без дополнительных
запросов к ЧЗ: состояние обновляется по итогу каждого scan/batch/balance.

Показывается:

- backend online/offline;
- режим mock/live;
- состояние True API (`unknown` / `ok` / `error`);
- `last_success` и реальная категория последней ошибки;
- список организаций с признаком `token_configured`;
- возраст токена (`token_updated_at`).

### Token age

`token_updated_at` — когда токен был обновлён **нашим приложением**.
Это НЕ точный `expires_at`. Эвристика для отображения:

- до 8 часов — информационно;
- 8–10 часов — «скоро может потребоваться обновление»;
- более 10 часов — «проверьте доступ к ЧЗ».

Фактический `token_invalid`/`unauthorized` от True API имеет приоритет над
этой эвристикой.

---

## Автоматическое обновление kiosk

`/api/status` отдаёт `build_id`. Frontend опрашивает статус примерно раз в
6 секунд.

`build_id`:

- основной источник — Git short SHA текущего HEAD;
- fallback (без `.git`) — short hash исходников приложения (`*.py`, `*.js`,
  `*.css`, `*.html`).

Если `build_id` изменился:

- ставится pending reload;
- активный scan/batch/balance **не** прерывается;
- reload выполняется после завершения операции (повторный скан автоматически
  не запускается).

Cache busting — `app.js?v=<build_id>` и `style.css?v=<build_id>`. Это важно
для Fully Kiosk, который может держать страницу открытой сутками: при
обновлении приложения ТСД подхватывает свежий frontend без ручного reload.

---

## Архитектура

Проект сознательно простой: FastAPI, Uvicorn, httpx, Pydantic, Jinja2,
vanilla JS/CSS, файловое хранилище токенов. Без React, Redis, Celery,
микросервисов и внешней БД.

```text
ТСД / Browser
     |
     v
FastAPI (main.py)
     |
     v
Service / business policy (service.py, models.py)
     |
     v
TrueApiClient (trueapi.py)
     |
     v
ГИС МТ True API
```

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
templates/                  index.html, admin_token.html
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
| `ORGANIZATIONS`  | список `INN=Название;...`                   | 4 организации (см. `config.py`) |
| `TOKENS_FILE`    | путь к файлу токенов                        | `data/tokens.json`          |
| `BUILD_ID`       | явный build_id (опционально)                | не задан                    |

Секретные значения (ADMIN_KEY, токены) в коде и документации не хранятся.
`ADMIN_KEY` по умолчанию `change-me-in-production` — в таком виде доступ к
`/admin/*` запрещён (fail-closed).

---

## Token workflow

Обновление bearer-токена организации — через служебную страницу `/admin/token`
(ссылки на неё в основном интерфейсе нет; токен живёт ~10 часов по данным ГИС МТ).

Порядок:

1. На Windows-машине с КриптоПро CSP и действующей УКЭП запустить
   `refresh_trueapi_token.ps1`:

   ```powershell
   .\refresh_trueapi_token.ps1
   ```

2. Скрипт получает challenge (`GET /auth/key`), подписывает `data` УКЭП через
   `csptest.exe` и печатает `UUID` + `SIGNATURE`.
3. Открыть `/admin/token`, выбрать организацию.
4. Вставить `UUID` в первое поле, `SIGNATURE` — во второе, нажать «Обновить токен».
5. Backend обменивает `UUID` + `SIGNATURE` на bearer-токен
   (`POST /auth/simpleSignIn`) и начинает использовать его без перезапуска.

Bearer-токен хранится **только** на backend; frontend его не получает.
Пара `UUID` + `SIGNATURE` одноразовая и живёт считанные минуты — вставлять
свежий вывод скрипта сразу после запуска. Имя владельца сертификата УКЭП
задаётся переменной `$certName` в начале скрипта, реальные auth-данные в
репозиторий не попадают.

---

## Формат TokenStore

`data/tokens.json` — runtime secret, в `.gitignore`, не коммитится.

Поддерживаются два формата (оба читаются, запись всегда в новом):

Legacy:

```json
{
  "7700000000": "TOKEN"
}
```

Новый (с метаданными):

```json
{
  "7700000000": {
    "token": "...",
    "updated_at": "2026-09-11T05:30:00Z"
  }
}
```

Старые записи читаются как есть (`updated_at = null`). При следующем
обновлении токена запись мигрирует в новый формат. Права файла — `0600`.

---

## API endpoints

- `GET /` — интерфейс проверки КМ (рендер `templates/index.html`).
- `GET /api/status` — состояние backend, True API, организаций, `build_id`.
- `POST /api/scan` — проверка одного КМ.
- `POST /api/scan_batch` — проверка списка КМ (до 1000).
- `POST /api/balance` — баланс по GTIN (статусы EMITTED/APPLIED/INTRODUCED).
- `GET /admin/token` — служебная страница обновления токена (защищена).
- `POST /admin/token` — обмен UUID+SIGNATURE на bearer-токен (защищён).

---

## Mock-режим

Детерминированный mock без доступа к ЧЗ (`GIS_MODE=mock`). GTIN
`04640638345218` считается «нашим».

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

На момент последнего обновления README полный suite проходит успешно.

---

## Deployment

Production: `/root/gis_app`, systemd-юнит `gis-trueapi.service`
(автозапуск, `Restart=always`, `EnvironmentFile=` → `.env`, Uvicorn на
`0.0.0.0:8000`).

```bash
cd /root/gis_app
git pull
systemctl restart gis-trueapi.service
systemctl status gis-trueapi.service
```

Файлы `.env` и `data/tokens.json` живут только на сервере (в `.gitignore`).

---

## Security notes

- bearer-токены хранятся только на backend, frontend их не получает и не
  логирует;
- `data/tokens.json` в `.gitignore`, права файла `0600`;
- `/admin/*` защищён `ADMIN_KEY` (`hmac.compare_digest`, fail-closed);
- не логировать token / signature / ADMIN_KEY;
- приватные ключи УКЭП на сервере не хранятся (КриптоПро остаётся на Windows-ПК);
- в production — HTTPS через reverse proxy (Caddy/nginx), не выставлять Uvicorn
  голым портом.
