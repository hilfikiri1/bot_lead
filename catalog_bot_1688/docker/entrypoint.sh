#!/usr/bin/env bash
set -euo pipefail

# Optionally run Alembic migrations before starting a service.
# Set RUN_MIGRATIONS=false to skip (useful when several services start at once).
RUN_MIGRATIONS="${RUN_MIGRATIONS:-true}"

run_migrations() {
    echo "[entrypoint] Running database migrations..."
    for attempt in 1 2 3 4 5; do
        if alembic upgrade head; then
            echo "[entrypoint] Migrations applied."
            return 0
        fi
        echo "[entrypoint] Migration attempt ${attempt} failed; retrying in 3s..."
        sleep 3
    done
    echo "[entrypoint] Migrations failed after retries." >&2
    return 1
}

case "${1:-bot}" in
    bot)
        if [ "${RUN_MIGRATIONS}" = "true" ]; then
            run_migrations
        fi
        echo "[entrypoint] Starting Telegram bot..."
        exec python -m app.main
        ;;
    api)
        echo "[entrypoint] Starting health API..."
        exec uvicorn app.api.health:app --host "${HEALTH_API_HOST:-0.0.0.0}" --port "${HEALTH_API_PORT:-8000}"
        ;;
    migrate)
        run_migrations
        ;;
    *)
        exec "$@"
        ;;
esac
