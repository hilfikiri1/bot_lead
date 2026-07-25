# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System dependencies + fonts with Cyrillic and CJK coverage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu \
        fonts-noto \
        fonts-noto-cjk \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better layer caching).
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        "aiogram>=3.13,<4.0" \
        "playwright>=1.47,<2.0" \
        "openai>=1.51,<2.0" \
        "fastapi>=0.115,<1.0" \
        "uvicorn[standard]>=0.32,<1.0" \
        "sqlalchemy[asyncio]>=2.0.30,<3.0" \
        "alembic>=1.13,<2.0" \
        "asyncpg>=0.29,<1.0" \
        "pydantic>=2.9,<3.0" \
        "pydantic-settings>=2.5,<3.0" \
        "httpx>=0.27,<1.0" \
        "jinja2>=3.1,<4.0" \
        "pillow>=10.4,<12.0" \
        "structlog>=24.4,<25.0" \
        "tenacity>=9.0,<10.0" \
        "qrcode[pil]>=7.4,<9.0"

# Install Chromium and its OS dependencies for Playwright.
RUN playwright install --with-deps chromium

# Copy application code.
COPY . .

# Entrypoint runs migrations (when enabled) before the given command.
RUN chmod +x /app/docker/entrypoint.sh

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["bot"]
