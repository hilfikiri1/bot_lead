# Agent v5.0 — Digital Operations Director

Telegram становится единым операционным центром Buy & Bring Solutions: карточка проекта, inbox, контроль следующего шага, timeline, оценка лидов, диагностика Drive и безопасные интеграции.

## Architecture

```text
Telegram Handler
→ Intent Router (planner)
→ Project / Contact Resolver
→ UnifiedProjectService
→ NextAction / Inbox / Timeline / Assessment
→ Confirmation (pending_agent_actions)
→ Executor + Outbox
→ Audit / ProjectEvent
```

## Core modules

| Module | Path |
|--------|------|
| ContactResolver | `app/services/contact_resolver.py` |
| Drive diagnostics | `app/services/drive_diagnostics.py` |
| Unified project | `app/services/unified_project_service.py` |
| Timeline | `app/services/project_timeline_service.py` |
| Next action / inbox | `app/services/next_action_service.py` |
| Lead assessment | `app/services/lead_assessment_service.py` |
| Outbox | `app/services/outbox_service.py` |
| Sheets analytics | `app/services/sheets_analytics_service.py` |
| Document intelligence | `app/services/document_intelligence_service.py` |
| Calendar policy | `app/services/calendar_policy.py` |
| Conversation analysis | `app/services/conversation_analysis_service.py` |

## New Telegram commands

- `/plan` — план на сегодня
- `/inbox` — операционный inbox
- `/overdue`, `/without_next`, `/waiting_client`, `/waiting_us`, `/stale`
- `/history <номер>` — хронология
- `/drive_status` — диагностика Drive
- `/integration_status`, `/failed_actions`
- `/sheets_sync_preview` — dry-run нумерации Sheets
- `/assess` / «оценка лида»

## Safety

- External writes still require preview → confirm
- Gmail creates drafts only
- WhatsApp is manual (`wa.me`), not auto-sent
- Calendar only for precise timed events
- Partial Notion/Drive failures do not block the Kommo card
- No secrets in `/drive_status`

## Migration

`011_agent_v5_operations` — project_events, project_memories, lead_assessments, next_action_states, integration_operations, user_notification_preferences, sheets_lead_links, document_extractions, undo_operations, `project_artifacts.content_hash`

## Docs

- [AGENT_V5_ACCEPTANCE.md](AGENT_V5_ACCEPTANCE.md)
- [RAILWAY_AGENT_V5.md](RAILWAY_AGENT_V5.md)
- [VALIDATION_AGENT_V5.md](VALIDATION_AGENT_V5.md)
