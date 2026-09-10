# GIS MT True API — внутреннее веб-приложение проверки кодов маркировки «Честный ЗНАК»

Внутренний инструмент для сотрудников компании-дистрибьютора электронных
компонентов. Работает через браузер на Windows-ПК и Android-ТСД со встроенным
2D-сканером.

## Возможности

- Проверка одного КМ (Data Matrix) со сканера — автофокус, Enter запускает проверку.
- Пакетная проверка (текстовый ввод, вставка списком/колонкой из Excel).
- Баланс GTIN по статусам EMITTED / APPLIED / INTRODUCED (независимо), по всем организациям.
- Определение «наш / чужой» КМ по ownerInn против справочника организаций.
- Несколько юридических лиц, каждое со своим токеном.
- Mock-режим (работает без доступа к ЧЗ) и Live-режим (True API ГИС МТ).
- Обновление токенов организаций через служебную страницу `/admin/token`.
- Kiosk UI для Android-ТСД: крупные карточки результата, автофокус, звук/вибрация,
  защита от дубля Enter, нижняя навигация, Membership-friendly CSS.
- Светлая/тёмная тема (CSS-переменные, сохранение в localStorage).
- Постоянная status bar с мониторингом состояния backend и True API (без доп. запросов к ЧЗ).

## Стек

Python 3.12+, FastAPI, Uvicorn, httpx, Pydantic, Jinja2, vanilla JS. Без React/Redis/Celery/микросервисов.

## Структура

```
gis_app/
    main.py             FastAPI-приложение + REST API + /admin/token
    config.py           конфигурация (.env), справочник организаций
    service.py          бизнес-логика (scan / batch / balance / exchange)
    trueapi.py          интерфейс MarkingApiClient + live-клиент True API
    mockclient.py       mock-реализация клиента (для разработки/тестов)
    datamatrix.py       парсер Data Matrix (GS1, FNC1, GTIN checksum)
    models.py           модели, статусы, категории ошибок
    organizations.py    справочник организаций
    tokens.py           хранилище токенов (файловое)
    schemas.py          Pydantic-схемы REST
    static/             index.html, app.js, style.css
    templates/          admin_token.html
    123.ps1             PowerShell: получение UUID+SIGNATURE (УКЭП через КриптоПро)
    tests/              pytest (parser, service, trueapi client)
    data/               tokens.json (runtime, в .gitignore) + .gitkeep
```

## Запуск

```bash
cd gis_app
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # при необходимости поправить

# mock-режим (без ЧЗ):
GIS_MODE=mock uvicorn main:app --host 0.0.0.0 --port 8000

# live-режим:
GIS_MODE=live uvicorn main:app --host 0.0.0.0 --port 8000
```

Приложение: http://localhost:8000/
Статус: http://localhost:8000/api/status

## Тесты

```bash
pytest -q
```

## Обновление токена организации (через 123.ps1)

Токены живут ~10 часов (по данным ГИС МТ). Обновление — через служебную страницу
`/admin/token` (на неё НЕТ ссылки в основном интерфейсе).

Порядок:

1. На Windows-машине с установленным КриптоПро CSP запустить `123.ps1`.
   Скрипт сам запрашивает `GET /auth/key`, подписывает `data` УКЭП и печатает
   UUID + SIGNATURE.
2. Открыть `/admin/token`, выбрать организацию.
3. Вставить UUID в первое поле, SIGNATURE — во второе.
4. Нажать «Обновить токен».

Backend обменивает UUID+SIGNATURE на bearer-токен через `POST /auth/simpleSignIn`
и сразу начинает его использовать **без перезапуска**.

ВАЖНО: пара UUID+SIGNATURE одноразовая и живёт считанные минуты. Вставлять
свежий вывод 123.ps1 немедленно после запуска скрипта.

## Mock-режим

Для разработки/демонстрации. Управляется `GIS_MODE=mock`.

Детерминированные тестовые КМ (GTIN `04640638345218` считается «нашим»):

- `010464063834521821<SERIAL>` — APPLIED, наш, 400 шт.
- serial с `EMITTED` → статус EMITTED; с `INTRODUCED` → INTRODUCED.
- serial с `Q<число>` → quantityInPack = числу (например `Q500` → 500).
- serial с `NOTFOUND` → «КМ не найден».
- чужой GTIN (например `07712345678907`) → «чужой».

## Конфигурация (.env)

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `GIS_MODE` | `mock` или `live` | `mock` |
| `GIS_BASE_URL` | базовый URL ГИС МТ | `https://markirovka.crpt.ru` |
| `ADMIN_KEY` | ключ доступа к `/admin/*` | `change-me` (замените) |
| `PRODUCT_GROUPS` | товарные группы для `cises/search` | `radio` |
| `ORGANIZATIONS` | список `INN=Название;...` | 4 организации |
| `TOKENS_FILE` | путь к tokens.json | `data/tokens.json` |

## Windows / PowerShell (123.ps1)

`123.ps1` — PowerShell-скрипт для получения UUID + SIGNATURE на Windows-машине
с установленным КриптоПро CSP и действующей УКЭП. Последовательность:

True API challenge (`GET /auth/key`) → подпись `data` УКЭП через `csptest.exe`
(`-sfsign -sign -base64 -add`) → вывод `UUID` + `SIGNATURE`.

Полученные значения вставляются в `/admin/token` (по одному — UUID в первое поле,
SIGNATURE во второе). Реальные auth-данные в репозиторий не попадают: имя владельца
сертификата УКЭП задаётся переменной `$certName` в начале скрипта.

## Deployment

Текущая схема боевого развёртывания — systemd-сервис + FastAPI/Uvicorn:

- systemd юнит `gis-trueapi.service` (автозапуск, рестарт при падении),
  `EnvironmentFile=` указывает на локальный `.env`.
- `GIS_MODE=live`, слушает `0.0.0.0:8000`.
- Доступ по `http://<SERVER_IP>:8000`.

Файлы `.env` и `data/tokens.json` живут только на сервере (в `.gitignore`).
Публичный IP VPS и секреты в документацию не включаются.

## Безопасность

- `.env` и `data/tokens.json` — в `.gitignore`, не коммитить.
- Токены не отдаются фронтенду и не логируются.
- Приватные ключи УКЭП на сервере не хранятся (КриптоПро остаётся на Windows-ПК).
- `/admin/*` защищён `ADMIN_KEY` (fail-closed: без ключа доступ запрещён).
- В production: HTTPS + reverse proxy (Caddy/nginx). Не выставляйте Uvicorn голым портом.

## Архитектурные принципы

1. Backend — единственная точка общения с True API. Frontend токенов не видит.
2. Организации подключаются конфигурацией, без `if inn == "..."` в коде.
3. Один Data Matrix parser (datamatrix.py) — везде.
4. Один Service для скана, batch и balance.
5. Результаты проверки КМ **не кэшируются** — каждый скан даёт свежий ответ True API.
6. Connection pooling через единый httpx.AsyncClient.
7. Batch API (до 1000 КМ) везде, где доступно.
8. Каждый статус balance считается независимо. `quantityInPack` — отдельно от количества КМ.