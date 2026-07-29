# B&BS Operational Agent v2

This change adds a new operational layer without removing the existing call,
Calendar, Google Sheets, Kommo, or legacy Notion workflows.

## New commands

- `/digest` or `что делать сегодня` — rank open Kommo deals and create deduplicated Notion tasks.
- `/notion_test` or `проверь Notion` — validate the operational database schema.
- `/sync_leads` or `синхронизируй сделки Kommo` — upsert open Kommo deals into `Клиенты и проекты`.
- `/errors` or `последние ошибки` — show recent integration failures.
- `сделай КП для #123456` — generate a review-only commercial proposal draft.
- `подготовь запрос поставщику для #123456` — generate a supplier inquiry.
- `сделай каталог для #123456` — generate a catalog outline.
- `подготовь follow-up для #123456` — generate a client follow-up draft.

Drafts are never sent automatically.

## Railway variables

```env
NOTION_API_TOKEN=secret_...
NOTION_PROJECTS_DATA_SOURCE_ID=fcb024d7-ac9f-4948-a2e9-715fa011c712
NOTION_TASKS_DATA_SOURCE_ID=56d63b07-c16c-4077-a78f-0b44741d58f0
NOTION_OFFERS_DATA_SOURCE_ID=e463541b-a37f-4d9e-a669-34b00f29543d
NOTION_CATALOGS_DATA_SOURCE_ID=c48bfbd8-3252-4d87-8bc0-e71486d5f012
NOTION_COMMUNICATIONS_DATA_SOURCE_ID=f99276f0-f95d-4aeb-8a5b-71fda8195492
NOTION_SYNC_ENABLED=true
DIGEST_MAX_ITEMS=10
NATURAL_COMMAND_ROUTER_ENABLED=true
```

The Notion integration associated with the token must be connected to the
`B&BS — Операционная система` page and every related database.

## Deployment

```bash
pip install -r requirements.txt
alembic upgrade head
pytest -q tests/test_operational_command_router.py \
  tests/test_operational_notion_schema.py \
  tests/test_digest_rules.py
```

After deployment, run `/notion_test`, then `/digest`.
