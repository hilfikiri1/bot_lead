# Agent v4.3 — Marketing Lead Registry Sync

Agent v4.3 separates marketing qualification in Google Sheets from operational
sales stages in Kommo.

## Contract

- Google Sheets column `W` is marketing-owned and never synchronized from
  Kommo.
- `SQL` means a qualified target client.
- `MQL` means a lead that may become qualified after additional discovery.
- Other dropdown values remain valid marketing outcomes and are not mapped to
  Kommo stages.
- Confirmed synchronization may write only:
  - `X` — concise marketing history;
  - `Y` — internal sequential number;
  - Kommo lead name — `<number> - <short product>`.

## Number assignment

1. Existing pairs are resolved by the exact number in `Y` and the Kommo title.
2. Unresolved leads are matched by exact phone, email, or an unambiguous name.
3. Product-only matches are never accepted automatically.
4. The next number is the highest numeric value found in `Y` or a Kommo title,
   plus one.
5. Multiple new rows are numbered in spreadsheet row order in one preview.
6. Before writing, the bot checks the row fingerprint, old number, old comment,
   and current Kommo title again.

## Marketing comment

The generated `X` value is a stable snapshot containing:

- client;
- requested product;
- budget, contact channel, and region when available;
- the independent marketing status from `W`;
- the existing human-written reason;
- current Kommo stage;
- a short chronology from recent common Kommo notes.

An existing human comment is preserved as `Основание`. Once the bot has
structured the comment, later runs rebuild it without recursively duplicating
the previous text.

## Safety

- Preview is read-only.
- Periodic scheduler only notifies.
- Every external write requires explicit Telegram confirmation.
- A stale preview is rejected.
- Sheet number/comment writes happen before Kommo rename so an internal number
  cannot be lost or reused if a CRM rename fails.
- A failed Kommo rename remains visible on the next sync and can be retried.
- No database migration is required; Agent v4.2 head remains
  `010_agent_v4_2_workspace`.

## Acceptance test

1. Keep `GOOGLE_SHEETS_WRITE_ENABLED=false`.
2. Run `/status_sync`.
3. Confirm the report says that `W` is not compared or changed.
4. Check a new row without `Y`; the preview must show the next free number and
   a Kommo title.
5. Check a numbered row with a Kommo note; the preview may propose only `X`.
6. Enable confirmed writes and grant the service account Editor access.
7. Confirm one sync.
8. Verify:
   - `W` did not change;
   - `X` contains the concise history;
   - `Y` contains the assigned number;
   - the Kommo title starts with the same number.
9. Run `/status_sync` again; unchanged rows must not be proposed again.

## Automated verification

```bash
python -m compileall -q app tests
alembic heads
pytest -q
git diff --check
```
