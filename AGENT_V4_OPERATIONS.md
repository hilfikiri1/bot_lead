# Agent v4 — Operations & Google Drive

Agent v4 extends the unified B&BS AI Agent with project operations across Kommo, Notion and Google Drive.

## Capabilities

| Feature | Intent / trigger | Confirmation required |
|---------|------------------|----------------------|
| Create Drive project | `создай проект в drive` | Yes |
| Link Kommo + Notion + Drive | `свяжи notion с проектом` | Yes |
| Project snapshot | `что происходит по проекту` | No (read-only) |
| Upload file to project | Send document/photo in Telegram | Yes |
| AI cost report | `/costs` | No (read-only) |
| Morning/evening digest | Scheduler or `/today`, `/evening` | No (read-only) |

## Architecture

- `ProjectLink` — persistent mapping between Kommo lead, Notion page and Drive folder
- `pending_agent_actions` — all external writes go through Telegram confirmation
- `project_links` + `ai_usage_events` tables — migration `008_agent_v4_operations`

## Related docs

- [GOOGLE_DRIVE_PROJECTS.md](GOOGLE_DRIVE_PROJECTS.md) — Drive folder layout and env vars
- [VALIDATION_AGENT_V4.md](VALIDATION_AGENT_V4.md) — local validation steps
- [RAILWAY_AGENT_V4.md](RAILWAY_AGENT_V4.md) — Railway deployment checklist
