# B&BS AI Agent v3 — validation report

Base: `hilfikiri1/bot_lead` commit `95749d1` (`main`).

## Completed checks

- Python bytecode compilation: `python -m compileall -q app migrations` — passed.
- Alembic chain: `006_calendar_events -> 007_operational_agent_v2 -> 007_unified_agent_v3` — one head.
- Focused agent suite — **24 passed**.
- Complete repository suite — **111 passed** in the build environment.

The build container did not have the external Google, Redis and Celery SDKs installed. For the complete test collection, import-only no-op stubs were supplied outside the project directory. Those stubs are **not included** in either ZIP. On a real machine or Railway, install `requirements.txt` and rerun `pytest -q` against the actual SDKs.

## Safety checks covered

- external writes are staged and require a Telegram confirmation button;
- pending actions are bound to the Telegram user;
- repeated confirmation clicks do not execute an action twice;
- action expiry and rejection are handled;
- operational audit payloads redact common secret fields and bearer tokens;
- `/digest`, lead lookup and diagnostics remain read-only;
- calendar diagnostics do not create a test event;
- voice-call analysis does not write to Notion until confirmation;
- generated Notion tasks do not silently request a second Kommo write;
- ambiguous Kommo search results can be selected with safe callback buttons.
