# Agent v5.0 — Acceptance checklist (~60–90 min)

Use a **staging** bot and non-production Kommo/Drive/Notion/Sheets. Do not auto-send WhatsApp or Gmail.

## Prep

1. Deploy branch with `GOOGLE_DRIVE_ENABLED=true` (staging folders only).
2. Confirm `/health` and `/version` return `5.0.0`.
3. Log in as Owner/Admin.

## Scenario

1. Open an existing project (internal number or search).
2. Verify linked contact name + phone appear; WhatsApp link works; **no false “нет телефона”** when contact has a phone.
3. Open `/history <number>` — filters: Последние 5 / Переговоры / Задачи / Файлы / Решения / Показать ещё.
4. Upload a PDF → preview → confirm → appears in Drive + timeline.
5. Re-send the same PDF (same Telegram message / same hash) → no duplicate upload.
6. Upload Excel → classified / routed.
7. Upload a photo → classified as product photo (or other) with preview.
8. Send a voice note with several instructions → bundle preview.
9. Edit one staged action.
10. Confirm all remaining actions.
11. Verify Kommo task/note created.
12. Verify Drive file present.
13. Verify Notion memory/link if configured (or graceful warning if not).
14. Prepare Polish WhatsApp follow-up → `wa.me` opens; bot does **not** claim sent.
15. Mark as sent manually.
16. Create next action with due date.
17. `/without_next` no longer lists this project (or lists others).
18. `/overdue` lists only overdue items.
19. `/plan` shows compact daily plan.
20. `/digest` still works and includes sections.
21. `/sheets_sync_preview` shows matches / conflicts / formula skips — no write yet.
22. Confirm a numbering assignment only after preview (idempotent title prefix).
23. Log in as Manager.
24. Attempt to open another manager’s deal → denied.
25. Simulate Drive 403 (revoke SA access to a folder) → `/drive_status` shows **category**, not a generic dump.
26. `/integration_status` lists failed/retry items when present.
27. Retry a retryable outbox item (Admin).
28. Check audit / integration events.
29. Undo a reversible field change if available; confirm Undo also needs confirmation.
30. Confirm WhatsApp/Gmail were never auto-sent.

## Pass criteria

- All 30 steps completed without secrets in Telegram.
- Version endpoints show `5.0.0`.
- Regression suite green: `./scripts/validate_agent_v5.sh`.
