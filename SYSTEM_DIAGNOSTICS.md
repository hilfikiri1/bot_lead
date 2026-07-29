# B&BS System Diagnostics

## Цель

Вместо ручного поиска одной ошибки за раз система создаёт один диагностический пакет с единым Trace ID.

Пакет содержит:

- конфигурацию в формате `SET / EMPTY / MISSING`, без значений секретов;
- PostgreSQL и текущую Alembic revision;
- наличие ключевых таблиц;
- Redis;
- Telegram Bot API и webhook;
- Kommo account API;
- Google Sheets и дубли внутренних номеров;
- Google Drive read-only доступ, включая Shared Drive membership;
- Notion token и доступ к базам;
- WhatsApp Cloud API read-only доступ к Phone Number ID;
- последние IntegrationEvent за 60 минут;
- при указании проекта — Kommo, контакт, ProjectLink, Drive folder и внешний чат.

Диагностика не изменяет внешние сервисы. В локальную PostgreSQL записывается только одна санитизированная запись `system_diagnostics/read_only_audit` с Trace ID.

## Telegram

Полный аудит:

```text
/diag
```

Аудит с проверкой проекта:

```text
/diag 107
```

Справка:

```text
/diag help
```

Бот отправляет:

1. краткий результат в Telegram;
2. `BBS_diagnostic_<trace_id>.md` — читаемый отчёт;
3. `BBS_diagnostic_<trace_id>.json` — полный пакет для анализа.

В обращении по ошибке достаточно приложить JSON и написать Trace ID. Видео требуется только для визуальной проблемы интерфейса.

## Railway AI / HTTP

Endpoint защищён `X-Admin-Key`:

```text
GET /admin/diagnostics/run
GET /admin/diagnostics/run?project=107&recent_minutes=120
```

Railway AI не должен выводить значение `X-Admin-Key` в ответе.

Пример задачи для Railway AI:

```text
Выполни безопасный GET-запрос к /admin/diagnostics/run?project=107,
используя ADMIN_API_KEY из окружения как X-Admin-Key.
Не показывай ключ. Ничего не изменяй. Верни полный JSON-ответ.
```

## Request ID в Railway logs

Каждый HTTP-запрос получает `X-Request-ID`. В логах появляются:

```text
request_start request_id=req-... method=POST path=/webhook/telegram
request_complete request_id=req-... status=200 duration_ms=...
```

При исключении:

```text
request_failed request_id=req-... error=...
```

Request body, headers, токены и секреты не логируются.

## Новый процесс тестирования

### Этап 1 — до ручного теста

1. Запустить `/diag`.
2. Исправить все `FAIL`.
3. Оценить `WARN`.
4. Не начинать ручной тест, если Database, Telegram или Kommo имеют `FAIL`.

### Этап 2 — один контрольный проект

Запустить:

```text
/diag 107
```

Проверить:

- правильный Kommo ID;
- телефон и email;
- ProjectLink;
- правильную страну;
- Drive folder ID и доступ service account;
- Notion page;
- причину недоступности external chat history.

### Этап 3 — короткий функциональный прогон

На одном тестовом проекте:

1. открыть карточку;
2. открыть историю;
3. подготовить follow-up, но отменить;
4. изменить статус и подтвердить;
5. загрузить маленький PDF и подтвердить;
6. добавить заметку;
7. создать задачу;
8. повторно запустить `/diag <номер>`.

### Этап 4 — пакет для разбора

При любой ошибке не нужно пересказывать весь экран. Отправляются:

- JSON-файл диагностики;
- Trace ID;
- одно предложение: что нажато и что ожидалось;
- скриншот только если проблема визуальная.

## Границы

Автоматическая диагностика не может подтвердить визуальное расположение кнопок или поведение системного экрана импорта контактов iPhone. Для таких случаев достаточно короткой записи экрана 20–40 секунд, а не полного видео на 15 минут.
