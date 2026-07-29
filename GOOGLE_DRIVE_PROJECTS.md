# Google Drive Projects

## Folder layout

Each Kommo lead gets a project key:

- With internal B&BS number: `BBS-{COUNTRY}-{NNNN}` (e.g. `BBS-PL-0120`)
- Without internal number: `BBS-{COUNTRY}-KOMMO-{id}`

Under `GOOGLE_DRIVE_PROJECTS_FOLDER_ID`, the agent creates:

```
BBS-PL-0120 — Client product name/
  01 Запрос клиента
  02 Техническое задание
  ...
  99 Архив проекта
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_DRIVE_ENABLED` | Yes | `true` to enable Drive operations |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Yes | Same SA as Calendar (or `_BASE64`) |
| `GOOGLE_DRIVE_ROOT_FOLDER_ID` | Yes | Shared Drive or root folder ID |
| `GOOGLE_DRIVE_PROJECTS_FOLDER_ID` | Yes | Parent folder for all projects |
| `GOOGLE_DRIVE_INBOX_FOLDER_ID` | Optional | Future inbox routing |
| `GOOGLE_DRIVE_PROJECT_TEMPLATE_FOLDER_ID` | Optional | Reserved for template copy |

**Never hardcode folder IDs in Python.** Configure them only via Railway/env.

## Service account access

1. Create or reuse a Google service account with Drive API enabled.
2. Share the root/projects folders with the SA email (Editor).
3. Set `GOOGLE_DRIVE_ENABLED=true` and folder IDs on Railway.

## File uploads

1. Select a lead in the agent (digest button or search).
2. Send a document or photo in Telegram.
3. Confirm the staged upload — the file lands in the matching subfolder (caption can mention subfolder name).
