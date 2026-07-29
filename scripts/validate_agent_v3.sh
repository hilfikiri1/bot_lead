#!/usr/bin/env bash
set -euo pipefail

python -m compileall -q app migrations
pytest -q \
  tests/test_agent_planner.py \
  tests/test_agent_digest.py \
  tests/test_agent_safety.py \
  tests/test_agent_notion_schema.py \
  tests/test_agent_voice_safety.py \
  tests/test_agent_executor_safety.py

echo "Agent v3 validation passed."
