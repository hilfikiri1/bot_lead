# Railway rollout — Agent v5.1 + Kaizen Journal

## 1. Pre-deploy

- Merge PR only after CI green.
- Backup Postgres.
- Confirm current Alembic head is `013_whatsapp_cloud_messages` before this release.
- Keep the feature flags disabled during the first deploy.

## 2. Env vars to add

Existing Agent v5 variables:

```text
AGENT_STALE_DAYS_DEFAULT=7
AGENT_SOURCE_TIMEOUT_SECONDS=8
GOOGLE_DRIVE_INTERNAL_FOLDER_ID=
GOOGLE_DRIVE_RESTRICTED_FOLDER_ID=
GOOGLE_DRIVE_EXTERNAL_FOLDER_ID=
```

Kaizen journal:

```text
AGENT_EVENING_REFLECTION_ENABLED=false
AGENT_EVENING_REFLECTION_HOUR=19
AGENT_EVENING_REFLECTION_REMINDER_HOURS=1

AGENT_WEEKLY_REVIEW_ENABLED=false
AGENT_WEEKLY_REVIEW_WEEKDAY=6
AGENT_WEEKLY_REVIEW_HOUR=19
AGENT_WEEKLY_REVIEW_MIN_DAILY_ENTRIES=2

AGENT_DIGEST_TIMEZONE=Europe/Warsaw
MANAGER_TIMEZONE=Europe/Warsaw
```

`0 = Monday`, `6 = Sunday`.

Existing Drive, Sheets, Kommo and Notion variables remain required as in v4.2. The diary itself needs only PostgreSQL, Telegram and OpenAI. Notion is optional until confirmed improvement cards are created.

## 3. Deploy order

1. Deploy code with both kaizen flags still `false`.
2. Run `alembic upgrade head` and verify head `014_kaizen_journal_entries`.
3. Verify `/health` reports version `5.1.0`.
4. Run `/diag` and `/notion_test` read-only.
5. Smoke `/plan`, `/inbox`, `/evening` and `/week` manually.
6. Confirm a voice reflection does not create a local client, lead or Kommo draft.
7. Set `AGENT_EVENING_REFLECTION_ENABLED=true`.
8. Keep `AGENT_EVENING_DIGEST_ENABLED=false` unless reflection is intentionally disabled; reflection has priority when both are true.
9. After several daily entries, set `AGENT_WEEKLY_REVIEW_ENABLED=true`.

## 4. Production smoke

1. `/evening` → invitation with three buttons.
2. Send one text reflection → compact summary and one daily DB row.
3. `Дополни дневник: ...` → same row updated, no duplicate.
4. Start `/evening`, send voice → journal saved, client-call pipeline not started.
5. `/today` while reflection is pending → command works and is not stored as diary text.
6. `Напомнить через час` → one reminder only.
7. `Пропустить сегодня` → scheduled invitation is not repeated.
8. `/week` → report works even if Kommo or Notion is temporarily unavailable.
9. Weekly `Создать в Notion` → first shows PendingAgentAction preview.
10. Cancel once, then confirm once; repeat confirmation must not create duplicates.

## 5. Notion Tasks preparation

Use the existing `NOTION_TASKS_DATA_SOURCE_ID`. Recommended properties:

- title: `Задача`;
- `Тип`: option `Improvement`;
- `Статус`: option `Todo`;
- `Источник`: option `Kaizen`;
- `External ID`: rich text;
- `Срок`: date, optional.

Create the board manually as documented in `KAIZEN_JOURNAL.md`. The API does not create or rename views/statuses.

## 6. Rollback

1. Disable `AGENT_EVENING_REFLECTION_ENABLED` and `AGENT_WEEKLY_REVIEW_ENABLED` first.
2. Redeploy the previous Railway image/commit.
3. Prefer keeping `kaizen_journal_entries` for audit/history.
4. Use `alembic downgrade 013_whatsapp_cloud_messages` only when the new table must be removed and after backup.

## 7. Do not

- Auto-merge to main without review.
- Enable scheduled reflection before migration `014` is applied.
- Auto-send Gmail/WhatsApp.
- Write daily journal entries to Notion.
- Create improvement cards without Telegram confirmation.
- Hardcode tokens, folder IDs or client/project data in code.
