# Phase 2 — Google Calendar, неразобранные сделки, Notion и голосовые команды

Дата сборки: 03.07.2026

## Что изменено

### 1. Google Calendar (production)

- Провайдер по умолчанию: `CALENDAR_PROVIDER=google` (service account).
- Пошаговый мастер `📅 Запланировать` в Telegram: тип события → дата → время → длительность → напоминание → предпросмотр → подтверждение.
- Событие создаётся в Google Calendar и параллельно — задача в Kommo по сделке.
- Идемпотентность через таблицу `calendar_events` и `idempotency_key` — повторное подтверждение не дублирует событие.
- Команды `/calendar_test` и `/calendar_test_write` для диагностики доступа и записи.
- Кнопка `📅 Проверить календарь` в главном меню.
- iCloud CalDAV сохранён как legacy (`CALENDAR_PROVIDER=icloud`) с `.ics` fallback.

### 2. Неразобранные сделки Kommo + Google Sheets

- Кнопка `📥 Неразобранные сделки` — только сделки в этапе **Incoming leads** (без внутреннего номера `^\d+\s*-\s*.+$`).
- Сопоставление со строкой реестра Google Sheets по телефону, email, имени клиента или компании.
- Сокращение польского названия товара (колонка P) до русского (детерминированные правила + OpenAI fallback).
- Предпросмотр переименования: `{Y} - {краткий товар RU}` → подтверждение → запись в Kommo.
- Аудит в таблице `spreadsheet_lead_mappings` (старый/новый name, match method, row number).
- Опциональный фильтр воронки: `KOMMO_UNREVIEWED_PIPELINE_ID`. Этап по умолчанию ищется по имени `Incoming leads` (`KOMMO_UNREVIEWED_STATUS_NAME`) или задаётся явно через `KOMMO_UNREVIEWED_STATUS_ID`.

### 3. Редактирование сделок Kommo в Telegram

- Из карточки сделки — изменение названия, бюджета, воронки, этапа, контактных полей.
- Список открытых сделок можно ограничить одной воронкой: `KOMMO_MENU_PIPELINE_ID`.
- Предпросмотр перед сохранением, понятные ошибки при устаревшем состоянии.

### 4. Notion workspace

- Автосинхронизация после анализа разговора: Clients, Leads, Calls (+ Task при наличии `NOTION_TASKS_DATABASE_ID`).
- `notion_page_id` сохраняется в `clients`, `leads`, `voice_notes` для повторных обновлений.
- Утренний дайджест: команда `/digest`, кнопка `☀️ Дайджест` в главном меню или голосовая команда «дайджест».
- CSV-шаблоны для первичного импорта: `notion-import/` и `notion-import-bbs.zip`.
- Календарные напоминания работают **без** Notion Tasks DB.

### 5. Голосовые и текстовые команды менеджера

- `VOICE_COMMAND_MODE=true` (по умолчанию): короткие голосовые/текстовые инструкции боту обрабатываются как команды, а не как разговор с клиентом.
- Распознавание намерения через OpenAI (`command_router_service.py`): поиск клиента/сделки, задачи, напоминания, календарь, Notion-заметки, дайджест.
- Полный AI-анализ разговора — только через кнопку `📥 Подготовить новый лид` / `🎙 Новый разговор`.

### 6. Надёжность deployment

- Миграции Alembic применяются автоматически при старте (`app/db_migrations.py`).
- Пустые env-переменные для опциональных Kommo ID (`KOMMO_*_ID=`) больше не ломают старт контейнера.
- Ошибки обработки аудио показываются менеджеру в Telegram, а не «зависают» молча.

### 7. Безопасность (наследие Phase 1 + дополнения)

- Service account JSON для Google Calendar/Sheets — только через env (`GOOGLE_SERVICE_ACCOUNT_JSON` или `_BASE64`), не в Git.
- Google Sheets — read-only scope.
- Секреты и транскрипты не логируются в production.

## Миграции базы данных

Цепочка после Phase 1:

```text
004_notion_integration
005_spreadsheet_lead_mappings
006_calendar_events
```

| Миграция | Что добавляет |
|----------|---------------|
| `004_notion_integration` | `notion_page_id` в `clients`, `leads`, `voice_notes` |
| `005_spreadsheet_lead_mappings` | Таблица аудита переименований из Sheets |
| `006_calendar_events` | Таблица событий календаря + уникальный `idempotency_key` |

Перед deployment рекомендуется backup PostgreSQL.

Миграции применяются:
1. автоматически при старте web-сервиса;
2. опционально вручную: `alembic upgrade head`.

## Railway

### Web service `bot_lead`

Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Pre-deploy command (рекомендуется, дублирует auto-migrate):

```bash
alembic upgrade head
```

### Celery worker `passionate-vibrancy`

Start command:

```bash
celery -A app.celery_app:celery_app worker --loglevel=info -Q voice_notes --concurrency=2
```

При `AUDIO_PROCESSING_MODE=direct` worker нужен только для очереди (минимум: `DATABASE_URL` + Redis).

### Обязательные переменные (базовый CRM)

```env
APP_ENV=production
LOG_LEVEL=INFO
DATABASE_URL=
REDISHOST=
REDISPORT=6379
REDISPASSWORD=

TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
WEBHOOK_BASE_URL=
ALLOWED_TELEGRAM_USER_IDS=

OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

KOMMO_BASE_URL=
KOMMO_ACCESS_TOKEN=

ADMIN_API_KEY=<длинный случайный секрет>
EXPOSE_API_DOCS=false
ENABLE_GOOGLE_OAUTH_ROUTES=false
AUDIO_PROCESSING_MODE=direct
```

### Google Calendar (рекомендуется)

```env
CALENDAR_PROVIDER=google
GOOGLE_CALENDAR_AUTH_MODE=service_account
GOOGLE_CALENDAR_ID=<ID календаря из настроек Google, не "primary">
GOOGLE_CALENDAR_NAME=BBS Работа
GOOGLE_CALENDAR_TIMEZONE=Europe/Warsaw
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=<base64 от service account JSON>
```

На macOS для base64:

```bash
base64 -i service-account.json | tr -d '\n'
```

Календарь нужно расшарить на email service account (`client_email` из JSON) с правом **Make changes to events**.

### Google Sheets — реестр лидов (для неразобранных сделок)

```env
GOOGLE_SHEETS_SPREADSHEET_ID=<ID из URL таблицы>
GOOGLE_SHEETS_WORKSHEET_NAME=<имя листа>
GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON=<тот же JSON или оставьте пустым — возьмётся из GOOGLE_SERVICE_ACCOUNT_JSON_BASE64>
GOOGLE_SHEETS_PHONE_COLUMN=O
GOOGLE_SHEETS_PRODUCT_COLUMN=P
GOOGLE_SHEETS_LEAD_NUMBER_COLUMN=Y
```

Таблицу расшарить на service account с правом **Viewer**.

### Notion (опционально)

Базы **не должны** находиться в разделе **Private**. Перенесите Clients, Leads, Calls и Tasks в teamspace команды и подключите каждую к интеграции **Buy Bring Bot** (`⋯ → Connections`).

```env
NOTION_API_TOKEN=
NOTION_AUTO_SYNC=true
NOTION_CLIENTS_DATABASE_ID=
NOTION_LEADS_DATABASE_ID=
NOTION_CALLS_DATABASE_ID=
NOTION_TASKS_DATABASE_ID=
VOICE_COMMAND_MODE=true
MORNING_DIGEST_ENABLED=true
MORNING_DIGEST_HOUR=8
```

Как получить database ID: откройте базу в Notion → **Copy link** → ID из URL (32 символа с дефисами). Это ID **базы**, не обычной страницы.

### Kommo — опциональные фильтры

```env
KOMMO_MENU_PIPELINE_ID=
KOMMO_UNREVIEWED_PIPELINE_ID=
KOMMO_UNREVIEWED_STATUS_ID=
```

Пустые значения допустимы — фильтр не применяется.

### iCloud (только если `CALENDAR_PROVIDER=icloud`)

```env
ICLOUD_USERNAME=
ICLOUD_APP_SPECIFIC_PASSWORD=
ICLOUD_CALENDAR_NAME=BBS Работа
```

## Проверка после deployment

### Базовый smoke test

1. `/menu` — главное меню на русском.
2. `🔌 Проверить Kommo` — соединение с CRM.
3. Короткое тестовое аудио → `/jobs` — статус обработки.
4. `📥 Подготовить новый лид` — предпросмотр и создание без дубля.

### Google Calendar

5. `/calendar_test` — чтение календаря, список ближайших событий.
6. `/calendar_test_write` — тестовая запись и удаление probe-события.
7. Открыть сделку → `📅 Запланировать` → пройти мастер → событие в Google Calendar + задача в Kommo.
8. Повторно нажать «Создать» — дубль не должен появиться.

### Неразобранные сделки

9. `📥 Неразобранные сделки` — список сделок без внутреннего номера.
10. Выбрать сделку → сопоставление со Sheets → предпросмотр `{номер} - {товар}`.
11. Подтвердить → имя в Kommo обновлено, запись в `spreadsheet_lead_mappings`.

### Notion и голос

12. `☀️ Дайджест` или `/digest` — утренний дайджест (нужны `NOTION_API_TOKEN` и `NOTION_TASKS_DATABASE_ID`).
13. Голосом: «найди сделку …» — маршрутизация команды, не анализ разговора.
14. Голосом: длинный разговор с клиентом + кнопка анализа — полный AI-отчёт.

## Проверки сборки

В локальной среде выполнены:

```text
pytest -q — 70 passed
python -m compileall app migrations — passed
alembic heads — 006_calendar_events
```

Реальная запись в ваш Kommo, Google Calendar, Google Sheets, Notion и Telegram не запускалась из локальной среды без production-секретов.

## Что не входит в Phase 2

- Полная двусторонняя синхронизация PostgreSQL ↔ Kommo.
- Импорт готовых `.txt`/`.docx` транскриптов.
- Голосовые команды календаря через тот же пошаговый предпросмотр, что и кнопка `📅 Запланировать` (сейчас голос создаёт событие напрямую).
- Полное удаление iCloud CalDAV backend.

Эти функции остаются для Phase 3.

## Связанные PR (уже в `main`)

- #14 — Notion, голосовые команды, CalDAV improvements
- #15 — Неразобранные сделки + Google Sheets
- #16, #17 — Google Calendar + fix пустых Kommo env vars
