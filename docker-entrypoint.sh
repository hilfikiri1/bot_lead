#!/bin/bash
set -e

echo "Running Alembic migrations..."
alembic upgrade head

echo "Starting Babrik Solutions Catalog Bot..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-config /dev/null
