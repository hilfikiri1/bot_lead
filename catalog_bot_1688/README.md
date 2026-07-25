# Babrik Solutions — 1688 → PDF Catalog Telegram Bot

MVP Telegram bot that turns a single **1688.com** product link into a branded
**Babrik Solutions** PDF catalog.

The user sends a product link, the bot opens the page with Playwright, extracts
the product data (title, prices, MOQ, specs, images), downloads and cleans the
photos, asks OpenAI to translate & structure the text into Russian (Structured
Outputs / JSON Schema), renders a local HTML → **A4 PDF** with Playwright, and
sends the PDF back — then cleans up temporary files.

> **OpenAI never creates the PDF.** It only translates/structures text. The PDF
> is always produced locally from a Jinja2 HTML template + Playwright
> `page.pdf()`.

---

## Table of contents

1. [Architecture](#architecture)
2. [Project layout](#project-layout)
3. [Prerequisites](#prerequisites)
4. [Step-by-step setup](#step-by-step-setup)
   1. [Create a Telegram bot (BotFather)](#1-create-a-telegram-bot-botfather)
   2. [Get an OpenAI API key](#2-get-an-openai-api-key)
   3. [Fill in `.env`](#3-fill-in-env)
   4. [Add the logo](#4-add-the-logo)
   5. [Start PostgreSQL](#5-start-postgresql)
   6. [Run Alembic migrations](#6-run-alembic-migrations)
   7. [Install Playwright locally](#7-install-playwright-locally)
   8. [Manual 1688 login](#8-manual-1688-login)
   9. [Run locally](#9-run-locally)
   10. [Run with Docker Compose](#10-run-with-docker-compose)
   11. [Check `/health`](#11-check-health)
   12. [Send a test link](#12-send-a-test-link)
5. [Parsing limitations](#13-parsing-limitations)
6. [What to do about CAPTCHA](#14-what-to-do-about-captcha)
7. [Change brand colors](#15-change-brand-colors)
8. [Replace the logo](#16-replace-the-logo)
9. [Testing](#testing)
10. [Environment variables](#environment-variables)

---

## Architecture

```
Telegram user
     │  (product link)
     ▼
aiogram handlers ── DependencyMiddleware(TaskService)
     │
     ▼
TaskService ── per-user single active job · global semaphore · rate limit · DB job row
     │
     ▼
CatalogService (pipeline)
   1. url_validator     → SSRF-safe validation + redirect re-check
   2. BrowserManager    → Playwright Chromium + saved 1688 session
   3. Parser1688        → Layer 1 JSON/XHR · Layer 2 DOM · Layer 3 partial
   4. ImageDownloader   → fetch · Pillow normalize · dedupe (sha256 + phash)
   5. OpenAICatalogClient → Responses API + Structured Outputs (JSON Schema)
   6. CatalogRenderer   → Jinja2 HTML + CSS → Playwright page.pdf() (A4)
     │
     ▼
Telegram: send PDF document  →  cleanup temporary files
```

Two runnable processes share the same image/codebase:

- **bot** — aiogram long-polling worker + background cleanup service.
- **api** — FastAPI `/health` endpoint (also the future home of a webhook).

The bot works via **polling** for local dev; the FastAPI app is already in place
so switching to a **webhook** later is a small change.

## Project layout

```text
catalog_bot_1688/
  app/
    main.py                  # bot entrypoint (polling) + cleanup task
    config.py                # pydantic-settings configuration
    logging_config.py        # structlog setup
    exceptions.py            # domain errors with safe user messages
    bot/
      handlers/{start,product_link}.py
      middlewares/{dependency,logging}.py
      keyboards/              # reserved for future inline keyboards
      messages.py            # all Russian user-facing text
    parser/
      models.py              # ParsedProduct + Decimal price models
      url_validator.py       # SSRF-safe URL allowlist + redirect checks
      browser.py             # Playwright lifecycle, popups, captcha, scroll
      parser_1688.py         # multi-layer extraction (JSON → DOM → partial)
      selectors.py           # ALL selectors + JSON markers (extend here)
      image_downloader.py    # download + dedupe + limits
      session_manager.py     # saved 1688 storage state
    ai/
      openai_client.py       # Responses API + Structured Outputs + retries
      schemas.py             # CatalogContent + strict JSON Schema
      prompts.py             # system prompt + payload builder
    catalog/
      renderer.py            # HTML → A4 PDF via Playwright
      models.py              # render context / brand theme
      templates/catalog.html
      static/catalog.css
      static/logo.png        # <-- put your logo here (optional)
    database/
      base.py session.py models.py repositories.py
      migrations/            # Alembic env + versions
    services/
      catalog_service.py     # end-to-end pipeline
      task_service.py        # concurrency + DB + Telegram I/O
      cleanup_service.py     # delete expired PDFs / temp dirs
    api/health.py            # FastAPI health-check
    utils/{filenames,retry,images}.py
  scripts/
    login_1688.py            # manual 1688 login → storage state
    render_sample.py         # render a sample catalog for a visual check
  tests/                     # pytest (no live 1688 calls)
  storage/{temporary,output,browser}/
  docker/entrypoint.sh
  Dockerfile docker-compose.yml pyproject.toml alembic.ini
  .env.example .gitignore README.md
```

## Prerequisites

- Python **3.12**
- PostgreSQL 14+ (or use the bundled Docker service)
- For local (non-Docker) runs: Playwright Chromium + Cyrillic/CJK fonts

## Step-by-step setup

### 1. Create a Telegram bot (BotFather)

1. Open Telegram and start a chat with **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot` and follow the prompts (name + username ending in `bot`).
3. Copy the **token** BotFather gives you (looks like `123456:ABC-DEF...`).
4. Put it into `.env` as `TELEGRAM_BOT_TOKEN`.

### 2. Get an OpenAI API key

1. Go to <https://platform.openai.com/api-keys>.
2. Click **Create new secret key**, copy it.
3. Put it into `.env` as `OPENAI_API_KEY`.
4. Optionally set the model via `OPENAI_MODEL` (default `gpt-5-mini`).

### 3. Fill in `.env`

```bash
cd catalog_bot_1688
cp .env.example .env
# then edit .env with your editor
```

At minimum set `TELEGRAM_BOT_TOKEN` and `OPENAI_API_KEY`.

### 4. Add the logo

Put your logo image at:

```text
app/catalog/static/logo.png
```

(Path is configurable via `BRAND_LOGO_PATH`.) **If the file is missing the PDF
still renders** — a text logo with `BRAND_NAME` is used instead.

### 5. Start PostgreSQL

**Option A — Docker (just the DB):**

```bash
docker compose up -d db
```

**Option B — local Postgres:** create a database and set `DATABASE_URL` in
`.env`, e.g.:

```bash
createdb catalog_bot
# .env:
# DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/catalog_bot
```

### 6. Run Alembic migrations

```bash
# from catalog_bot_1688/
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
```

### 7. Install Playwright locally

```bash
python -m playwright install --with-deps chromium
```

Also install Cyrillic + CJK fonts so the PDF renders correctly (Debian/Ubuntu):

```bash
sudo apt-get install -y fonts-noto fonts-noto-cjk fonts-dejavu
```

(The Docker image installs these automatically.)

### 8. Manual 1688 login

1688 often requires an authenticated session. Run the helper on a machine with a
display (it opens a **visible** browser):

```bash
python scripts/login_1688.py
```

1. Log into your 1688 account in the opened window.
2. Open a product page to confirm you are logged in.
3. Return to the terminal and press **ENTER**.

The session is saved to `storage/browser/1688_storage_state.json` and re-used by
the bot. **This file contains cookies — it is git-ignored and never stored in the
database.** Copy it to the server / into the mounted `storage/browser` volume.

### 9. Run locally

```bash
source .venv/bin/activate
python -m app.main          # starts the polling bot + cleanup service
```

In another terminal, optionally start the health API:

```bash
uvicorn app.api.health:app --host 0.0.0.0 --port 8000
```

### 10. Run with Docker Compose

```bash
cp .env.example .env        # fill in tokens
# put your logo at app/catalog/static/logo.png (optional)
docker compose up --build
```

This starts **db**, **bot** (runs migrations first) and **api**. The
`storage/browser`, `storage/temporary` and `storage/output` folders are mounted
as volumes, so drop your `1688_storage_state.json` into `storage/browser/` first.

### 11. Check `/health`

```bash
curl http://localhost:8000/health
# {"status":"ok","database":"ok"}
```

### 12. Send a test link

1. Open your bot in Telegram, send `/start`.
2. Send a 1688 product link, e.g. `https://detail.1688.com/offer/XXXXXXXX.html`.
3. Watch the single status message update:
   `Открываю страницу 1688…` → `Загружаю фотографии…` →
   `Перевожу и подготавливаю описание…` → `Формирую PDF-каталог…` →
   `Каталог готов.`
4. The bot replies with a PDF named
   `Babrik_Solutions_<product>_<YYYY-MM-DD>.pdf`.

## 13. Parsing limitations

- 1688 pages are dynamic and change often. The parser is resilient (JSON → DOM →
  partial) but a layout change can still reduce the fields captured.
- Some pages require a logged-in session or trigger CAPTCHA (see below).
- Rate-limiting / anti-bot measures may block automated access from data-center
  IPs. Residential/authenticated sessions work best.
- Minimum viable result is **title + at least one image + source URL**. Missing
  price → the PDF shows *"Цена уточняется у поставщика."*; missing specs → the
  specifications section is simply omitted.
- **When 1688 changes its DOM/state, add new selectors and JSON markers in
  `app/parser/selectors.py`** — the parser iterates them in order, so no other
  code needs to change.

## 14. What to do about CAPTCHA

The bot **does not** try to bypass CAPTCHA and uses no anti-captcha services.
If a CAPTCHA or login wall is detected, the bot replies:

> «1688 запросил повторную авторизацию или проверку CAPTCHA. Администратору
> необходимо обновить сессию 1688.»

and logs an `AuthenticationRequiredError` / `CaptchaDetectedError`. **Fix:**
re-run `python scripts/login_1688.py` and refresh
`storage/browser/1688_storage_state.json`.

## 15. Change brand colors

Colors are configuration-driven (see `.env`):

```env
BRAND_PRIMARY_COLOR=#0B1F3A   # dark navy headings
BRAND_ACCENT_COLOR=#D8A34A    # gold accent
BRAND_TEXT_COLOR=#20242A      # body text
```

They are injected into the template as CSS variables
(`--brand-primary`, `--brand-accent`, `--brand-text`). For deeper styling edit
`app/catalog/static/catalog.css`.

## 16. Replace the logo

Replace `app/catalog/static/logo.png` (or point `BRAND_LOGO_PATH` elsewhere).
Recommended: transparent PNG ≥ 480 px wide. Set company contact fields with
`BRAND_NAME`, `BRAND_WEBSITE`, `BRAND_EMAIL`, `BRAND_PHONE`.

## Testing

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Tests never touch the live 1688 site. They cover URL validation / SSRF blocking,
filename normalization, Pydantic models, the Structured Output JSON Schema, price
handling, image de-duplication, catalog HTML/PDF rendering from fixtures, and
Layer-1 parsing of a saved HTML fixture. The PDF test auto-skips if Chromium is
not installed.

Render a sample catalog for a visual check:

```bash
python scripts/render_sample.py   # writes storage/output/_sample.pdf
```

## Environment variables

See [`.env.example`](.env.example) for the full list. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | – | BotFather token (**required**) |
| `OPENAI_API_KEY` | – | OpenAI key (**required**) |
| `OPENAI_MODEL` | `gpt-5-mini` | Model used for translation/structuring |
| `DATABASE_URL` | `postgresql+asyncpg://…/catalog_bot` | Async DB URL |
| `BRAND_*` | Babrik defaults | Branding / colors / contacts |
| `PLAYWRIGHT_HEADLESS` | `true` | Headless Chromium |
| `PLAYWRIGHT_TIMEOUT_SECONDS` | `45` | Page open timeout |
| `PLAYWRIGHT_STORAGE_STATE` | `storage/browser/1688_storage_state.json` | Saved session |
| `MAX_CONCURRENT_JOBS` | `2` | Global browser-job semaphore |
| `MAX_IMAGES` / `MAX_GALLERY_IMAGES` / `MAX_DETAIL_IMAGES` | `12/8/4` | Image caps |
| `MAX_IMAGE_SIZE_MB` / `MAX_TOTAL_DOWNLOAD_MB` | `10/100` | Download caps |
| `MIN_IMAGE_SIDE_PX` | `300` | Skip small images/icons |
| `PDF_RETENTION_HOURS` | `24` | How long generated PDFs are kept |
| `RATE_LIMIT_SECONDS` | `15` | Per-user request throttle |
| `LOG_LEVEL` | `INFO` | Logging level |
| `DEBUG_SAVE_PAGE` | `false` | Save screenshot+HTML on parse errors |

**Secrets** (bot token, OpenAI key, cookies) are never written to the database
and are git-ignored.
