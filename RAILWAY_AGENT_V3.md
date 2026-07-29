# Railway: настройки B&BS AI Agent v3

## Обязательные существующие переменные

Сохраняются текущие параметры проекта, включая:

- `DATABASE_URL`
- `REDIS_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `WEBHOOK_BASE_URL`
- `TELEGRAM_ALLOWED_USER_IDS`
- `OPENAI_API_KEY`
- `KOMMO_BASE_URL`
- `KOMMO_ACCESS_TOKEN`

Не копируйте секреты в Git и не добавляйте `.env` в коммит.

## Agent v3

```env
AGENT_ENABLED=true
AGENT_AUTO_VOICE_MODE=true
AGENT_PLANNER_MODEL=
AGENT_WRITER_MODEL=
AGENT_ACTION_TTL_MINUTES=30
AGENT_DIGEST_MAX_ITEMS=10
AGENT_SYNC_MAX_LEADS=50
AGENT_MEMORY_COMPACT_EVERY=20
AGENT_MEMORY_RECENT_MESSAGES=12
```

Пустой `AGENT_PLANNER_MODEL` и `AGENT_WRITER_MODEL` означают использование существующего `OPENAI_MODEL`.

## Operational Notion

```env
NOTION_API_TOKEN=secret_...
NOTION_PROJECTS_DATA_SOURCE_ID=fcb024d7-ac9f-4948-a2e9-715fa011c712
NOTION_TASKS_DATA_SOURCE_ID=56d63b07-c16c-4077-a78f-0b44741d58f0
NOTION_OFFERS_DATA_SOURCE_ID=e463541b-a37f-4d9e-a669-34b00f29543d
NOTION_CATALOGS_DATA_SOURCE_ID=c48bfbd8-3252-4d87-8bc0-e71486d5f012
NOTION_COMMUNICATIONS_DATA_SOURCE_ID=f99276f0-f95d-4aeb-8a5b-71fda8195492
```

Интеграция Notion должна иметь доступ к странице `B&BS — Операционная система` и ко всем связанным базам.

## Deployment

Проект уже содержит запуск Alembic при старте. Тем не менее после deployment проверьте логи на строку успешной миграции. В Railway Shell можно выполнить:

```bash
alembic current
alembic heads
```

Ожидаемый head:

```text
007_unified_agent_v3
```

## Проверка безопасности

- `/digest` не должен создавать задачи и записи.
- Генерация КП/писем не должна отправлять их.
- Kommo/Notion/Gmail/Calendar должны изменяться только после Telegram-подтверждения.
- Повторное нажатие на выполненную кнопку не должно выполнять операцию второй раз.
- `/errors` должен показывать технические ошибки без API-ключей и паролей.
