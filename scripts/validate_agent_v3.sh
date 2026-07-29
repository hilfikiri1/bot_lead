#!/usr/bin/env bash
set -euo pipefail

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi

"$PYTHON" -m compileall -q app migrations
"$PYTHON" -m alembic heads
"$PYTHON" -m pytest -q \
  tests/test_agent_planner.py \
  tests/test_agent_digest.py \
  tests/test_agent_safety.py \
  tests/test_agent_notion_schema.py \
  tests/test_agent_voice_safety.py \
  tests/test_agent_executor_safety.py

echo "Agent v3 validation passed."
