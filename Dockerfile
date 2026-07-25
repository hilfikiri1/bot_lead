FROM python:3.12-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright / Chromium system libs
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    libatspi2.0-0 libwayland-client0 \
    # Fonts: Cyrillic + CJK (Chinese/Japanese/Korean)
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    fonts-wqy-microhei \
    fonts-wqy-zenhei \
    # Tools
    curl ca-certificates \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
    aiogram==3.13.1 \
    fastapi==0.115.5 \
    "uvicorn[standard]==0.32.1" \
    playwright==1.49.0 \
    "openai==1.57.0" \
    "sqlalchemy[asyncio]==2.0.36" \
    alembic==1.14.0 \
    asyncpg==0.30.0 \
    "pydantic>=2.7.0" \
    "pydantic-settings>=2.3.0" \
    "httpx==0.28.1" \
    jinja2==3.1.4 \
    "pillow==11.0.0" \
    "structlog==24.4.0" \
    "aiofiles==24.1.0" \
    "python-dotenv==1.0.1"

# Install Playwright Chromium
RUN playwright install chromium --with-deps

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# Create storage directories
RUN mkdir -p storage/temporary storage/output storage/browser

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
