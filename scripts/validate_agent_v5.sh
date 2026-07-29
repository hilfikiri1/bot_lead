#!/usr/bin/env bash
set -euo pipefail

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python
fi

"$PYTHON" -m compileall -q app migrations
"$PYTHON" -m alembic heads
"$PYTHON" -m pytest -q \
  tests/test_agent_v5_operations.py \
  tests/test_agent_v4_2_project_workspace.py \
  tests/test_agent_v4_operations.py \
  tests/test_agent_lead_reference_ux.py

echo "Agent v5 validation passed."
