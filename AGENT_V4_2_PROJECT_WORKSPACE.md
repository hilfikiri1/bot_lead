# Agent v4.2 — Project Workspace

Agent v4.2 turns Telegram into a unified project workspace for Kommo, Notion,
Google Drive, and the agent audit log.

## Delivered scope

- Unified project card:
  - client, company, phone, email, and communication language;
  - Kommo pipeline/status, responsible manager, recent notes, and open tasks;
  - Notion project, tasks, and recent communications;
  - recent audited uploads and Drive files;
  - pending actions, missing links, blockers, and recommended next step;
  - direct Kommo, Notion, and Drive buttons.
- Project lookup by:
  - internal project number;
  - client name;
  - phone;
  - company;
  - product/title fragment.
- Smart file workflow:
  - PDF, Word, Excel, generic documents, and photos;
  - deterministic file classification and folder routing;
  - project-scoped filename proposal;
  - preview and explicit confirmation;
  - Drive upload, Notion file record, Kommo audit note, and local audit row;
  - duplicate Telegram delivery protection.
- Spoken/text project update bundle:
  - Kommo negotiation note;
  - Notion communication;
  - Kommo and Notion task with a deadline;
  - next-step update;
  - optional client-language follow-up;
  - individual confirmation or `Подтвердить всё`.
- Project actions:
  - update status;
  - add task;
  - upload file;
  - prepare follow-up;
  - open history, Drive, Kommo, or Notion.
- Extended `/digest`:
  - overdue tasks;
  - projects without a next step;
  - stale projects;
  - files uploaded in the last 24 hours;
  - pending actions;
  - missing Kommo/Notion/Drive links;
  - five highest-priority actions.
- Manager safety:
  - list/search/card results respect assigned Kommo user;
  - assignment is checked again immediately before confirmed writes;
  - a Viewer cannot stage or confirm writes;
  - audit records identify the preparing and executing Telegram user.

## Migration and deployment

The new Alembic head is:

```text
010_agent_v4_2_workspace
```

After merging and deploying:

```bash
alembic upgrade head
alembic heads
```

Expected output:

```text
010_agent_v4_2_workspace (head)
```

No new Railway environment variables are required. Existing Kommo, Notion,
Google Drive, Telegram, and OpenAI settings remain in use.

## Focused acceptance test

Use real projects but start with harmless reads and one disposable document.

1. Send `покажи проект 134`.
   - Check the client, language, status, manager, tasks, files, links, and next step.
2. Send `что по Maciej Walasek?`.
   - Check that the correct project opens or a safe candidate list appears.
3. Send `найди проект по компании MasterTech`.
4. Upload a PDF with:
   - `Это предложение производителя для проекта 134`.
   - Check the suggested name and `04 Прайсы фабрик`.
   - Cancel once and verify no external write occurred.
   - Send it again, confirm, and verify Drive, Notion, and Kommo.
5. Upload an Excel calculation and one photo.
   - Check `06 Расчёты и сравнение` and `05 Фото, видео и образцы`.
6. Send a voice message:
   - `По проекту 134 поговорил с клиентом. Ему нужны кормушки и поилки в одинаковом количестве, половина контейнера каждого товара. Подготовить расчёт до пятницы.`
   - Confirm one item separately, then use `Подтвердить всё`.
7. Open the project card again.
   - Check the new note, communication, task, file, and next step.
8. Prepare the follow-up.
   - Check PL/UA selection and open WhatsApp without automatic sending.
9. As the invited Manager, repeat project search for an assigned and an
   unassigned deal.
   - The assigned deal must work; the unassigned deal must not be exposed.
10. Run `/digest`.
    - Check uploaded files, pending actions, discrepancies, and the top five.

## Automated verification

Run the same checks as CI:

```bash
python -m compileall -q app migrations tests
alembic heads
pytest -q
git diff --check
```

The implementation branch was verified with PostgreSQL offline upgrade and
downgrade SQL generation for migration `010`.

## Audit checks

Useful read-only SQL after smoke testing:

```sql
SELECT id, kommo_lead_id, artifact_type, status, telegram_user_id,
       uploaded_by_telegram_user_id, drive_file_id, created_at
FROM project_artifacts
ORDER BY created_at DESC
LIMIT 20;

SELECT id, action_type, batch_group_id, status, telegram_user_id,
       approved_by_telegram_user_id, executed_by_telegram_user_id, created_at
FROM pending_agent_actions
ORDER BY created_at DESC
LIMIT 30;
```

Every external write remains behind explicit Telegram confirmation.
