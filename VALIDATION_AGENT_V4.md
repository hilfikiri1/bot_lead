# Agent v4 Validation

## Quick check

```bash
chmod +x scripts/validate_agent_v4.sh
./scripts/validate_agent_v4.sh
```

## Full suite (includes v3)

```bash
pytest -q
./scripts/validate_agent_v3.sh
./scripts/validate_agent_v4.sh
```

## Alembic

Head should be `008_agent_v4_operations`:

```bash
python -m alembic heads
python -m alembic upgrade head
```

## Manual smoke tests (staging)

1. `/digest` — sections Срочно / Требует внимания / Планово
2. `создай проект в drive по сделке №…` — preview + confirm
3. `свяжи notion с проектом` — preview + confirm
4. `что происходит по проекту` — snapshot with links
5. Send PDF with active lead — staged Drive upload
6. `/costs` — AI usage summary

All write operations must show a confirmation button before executing.
