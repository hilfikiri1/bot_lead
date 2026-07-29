# Railway rollout — Agent v5.0

## 1. Pre-deploy

- Merge PR only after CI green.
- Backup Postgres.
- Note current Alembic head (`010_agent_v4_2_workspace` before this release).

## 2. Env vars to add

```text
AGENT_STALE_DAYS_DEFAULT=7
AGENT_SOURCE_TIMEOUT_SECONDS=8
GOOGLE_DRIVE_INTERNAL_FOLDER_ID=
GOOGLE_DRIVE_RESTRICTED_FOLDER_ID=
GOOGLE_DRIVE_EXTERNAL_FOLDER_ID=
```

Existing Drive/Sheets/Kommo/Notion vars remain required as in v4.2.

## 3. Deploy order

1. Deploy code with Drive writes still limited / feature-flagged if needed.
2. `alembic upgrade head` → `011_agent_v5_operations`.
3. Verify `/health`, `/ready`, `/version` → `5.0.0`.
4. Run `/drive_status` (read-only).
5. Smoke `/plan`, `/inbox`, open one project card.
6. Enable morning/evening digests per user preference when ready.

## 4. Production smoke (15 min)

1. Open project with linked contact → phone + WhatsApp.
2. `/drive_status` categories look correct.
3. Stage (do not rush) one Kommo note → confirm.
4. `/sheets_sync_preview` without writing.
5. Manager cannot open foreign lead.

## 5. Rollback

1. Redeploy previous Railway image/commit.
2. `alembic downgrade 010_agent_v4_2_workspace` only if new tables must be removed (safe if unused).
3. Keep `project_events` / outbox rows if you need audit continuity — prefer forward fix over destructive downgrade when possible.

## 6. Do not

- Auto-merge to main without review.
- Auto-send Gmail/WhatsApp.
- Hardcode folder IDs or project №117 data in code.
