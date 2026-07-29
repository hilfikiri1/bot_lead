# Validation — Agent v5.0

```bash
chmod +x scripts/validate_agent_v5.sh
./scripts/validate_agent_v5.sh
pytest -q
python3 -m compileall -q app migrations
python3 -m alembic heads   # expect 011_agent_v5_operations
```

## Focused checks

- Contact resolver: linked contact phone → WhatsApp URL
- Drive 403 categories via `drive_diagnostics`
- `/plan`, `/inbox`, `/overdue`, `/without_next`
- Sheets numbering idempotency
- Calendar policy (timed call vs follow-up)
- Health version `5.0.0`
