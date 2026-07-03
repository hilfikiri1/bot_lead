# Buy & Bring Solutions — Voice Bot MVP

Automated voice note processing for B2B sourcing calls.  
A manager records a voice note after a client call → the system transcribes, analyses, and generates follow-up drafts.

---

## Architecture

```
Telegram Bot
    │  (voice note)
    ▼
FastAPI Webhook
    │
    ▼
Celery Worker (Redis queue)
    │
    ├── Download audio (Telegram API)
    ├── Save to storage (local / S3)
    ├── Transcribe (OpenAI Whisper)
    ├── Analyse (GPT-4o, JSON schema)
    ├── Save to PostgreSQL
    │      ├── clients
    │      ├── leads
    │      ├── voice_notes
    │      ├── ai_reports
    │      └── actions
    └── Send report to Telegram (with inline buttons)
            │
            ▼ (manager approves)
    ┌───────────────────────────────────┐
    │  Gmail Draft  │  Calendar Event   │
    │  WhatsApp*    │  CRM Save         │
    └───────────────────────────────────┘
    * Draft only — NOT sent automatically
```

---

## Services

| File | Responsibility |
|------|---------------|
| `telegram_service.py` | Download voice notes, send reports, handle callbacks |
| `transcription_service.py` | OpenAI Whisper transcription |
| `ai_analysis_service.py` | GPT-4o structured JSON analysis |
| `storage_service.py` | Local / S3 audio file storage |
| `gmail_service.py` | Create Gmail drafts (never auto-sends) |
| `calendar_service.py` | Create Google Calendar events after approval |
| `whatsapp_service.py` | Prepare WhatsApp drafts (disabled until verified) |
| `crm_service.py` | Save clients, leads, voice notes, reports to DB |
| `approval_service.py` | Route inline button callbacks to correct actions |

---

## Quick Start

### 1. Clone & configure

```bash
git clone <repo>
cd buybring
cp .env.example .env
# Edit .env with your credentials
```

### 2. Google OAuth setup

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → Enable **Gmail API** and **Google Calendar API**
3. Create OAuth 2.0 credentials (Desktop app or Web)
4. Download `credentials.json` → save to `credentials/google_oauth.json`
5. Add your email to the test users list (while in testing mode)

### 3. Telegram Bot setup

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token to `TELEGRAM_BOT_TOKEN` in `.env`
3. Set `WEBHOOK_BASE_URL` to your public HTTPS domain
   - For local dev, use [ngrok](https://ngrok.com): `ngrok http 8000`

### 4. Start with Docker Compose

```bash
docker-compose up --build
```

### 5. Authorize Google (first time)

```bash
# Open in browser:
http://localhost:8000/auth/google
# Follow the OAuth flow → credentials saved automatically
```

### 6. Register the Telegram webhook

The webhook auto-registers on startup if `WEBHOOK_BASE_URL` is set.  
To register manually:
```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://your-domain.com/webhook/telegram" \
  -d "secret_token=your_secret"
```

### 7. Run database migrations

```bash
docker-compose exec api alembic upgrade head
```

---

## Usage

1. Open your Telegram bot
2. Send a voice message describing a client call
3. The bot replies with a structured report:
   - Client details (name, company, language, phone, email)
   - Product, budget, country/city, urgency
   - What was covered in the call
   - Mistakes / weak points in the conversation
   - Missing questions to ask the client
   - Recommended next step
   - Email draft (in client's language)
   - WhatsApp message draft
   - Calendar follow-up event
4. Tap inline buttons to approve actions:
   - **✉️ Create Gmail draft** → creates draft in your Gmail
   - **📅 Add to Calendar** → creates event in Google Calendar
   - **💬 Send WhatsApp draft to me** → sends draft text to your Telegram
   - **💾 Save to CRM** → updates lead in database
   - **❌ Cancel** → no action taken

---

## Admin API

```
GET /admin/leads         — List all leads
GET /admin/leads/{id}    — Lead detail with voice notes, reports, actions
GET /admin/clients       — List all clients
GET /admin/reports       — List all AI reports
GET /health              — Health check
GET /docs                — Swagger UI
```

---

## Environment Variables

See `.env.example` for the full list.

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | ✅ | PostgreSQL async URL |
| `REDIS_URL` | ✅ | Redis connection |
| `TELEGRAM_BOT_TOKEN` | ✅ | From BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | ✅ | Random secret for webhook validation |
| `WEBHOOK_BASE_URL` | ✅ | Public HTTPS URL for your service |
| `OPENAI_API_KEY` | ✅ | OpenAI API key |
| `GOOGLE_CLIENT_ID` | ✅ | For Gmail + Calendar |
| `GOOGLE_CLIENT_SECRET` | ✅ | For Gmail + Calendar |
| `STORAGE_BACKEND` | ❌ | `local` (default) or `s3` |
| `WHATSAPP_ENABLED` | ❌ | `false` by default — enable after business verification |

---

## Running Tests

```bash
# In container
docker-compose exec api pytest tests/ -v

# Locally
pip install -r requirements.txt
pytest tests/ -v
```

---

## WhatsApp Integration

WhatsApp sending is **disabled by default** and requires:
1. Meta Business Account verification
2. WhatsApp Business API approval
3. `WHATSAPP_ENABLED=true` + `WHATSAPP_PHONE_NUMBER_ID` + `WHATSAPP_ACCESS_TOKEN` in `.env`

Until enabled, the "Send WhatsApp draft" button forwards the draft text to the manager's Telegram for manual copy-paste.

---

## Security Notes

- API keys are stored in environment variables only
- Gmail creates drafts only — never auto-sends
- WhatsApp is disabled by default
- All AI outputs logged with `raw_json` in `ai_reports`
- All user approvals logged in `actions` table
- Telegram webhook validated with secret token
- Retry logic on Whisper, OpenAI, Telegram, and Google APIs

---

## Project Structure

```
buybring/
├── app/
│   ├── main.py                  # FastAPI app + lifespan
│   ├── config.py                # Pydantic settings
│   ├── database.py              # SQLAlchemy async engine
│   ├── celery_app.py            # Celery configuration
│   ├── api/
│   │   ├── telegram.py          # Webhook endpoint
│   │   ├── admin.py             # Admin REST endpoints
│   │   └── auth.py              # Google OAuth flow
│   ├── models/
│   │   ├── client.py
│   │   ├── lead.py
│   │   ├── voice_note.py
│   │   ├── ai_report.py
│   │   └── action.py
│   ├── services/
│   │   ├── telegram_service.py
│   │   ├── transcription_service.py
│   │   ├── ai_analysis_service.py
│   │   ├── storage_service.py
│   │   ├── gmail_service.py
│   │   ├── calendar_service.py
│   │   ├── whatsapp_service.py
│   │   ├── crm_service.py
│   │   └── approval_service.py
│   └── tasks/
│       └── voice_note_tasks.py
├── migrations/
│   ├── env.py
│   └── versions/
│       └── 001_initial.py
├── tests/
│   └── test_core.py
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
├── requirements.txt
├── .env.example
└── README.md
```

---

## Phase 1 (2026-06-29)

В проект добавлены безопасное подтверждение создания лидов Kommo, защита от дублей, статусы аудио, русский AI-отчёт, обновлённый Telegram UX и защита admin API.

Подробности и инструкции Railway: [`PHASE1_RELEASE_NOTES.md`](PHASE1_RELEASE_NOTES.md).

## Phase 2 (2026-07-03)

Google Calendar (service account), неразобранные сделки Kommo с Google Sheets, редактирование сделок в Telegram, Notion auto-sync, голосовые команды менеджера, авто-миграции БД при старте.

Подробности, env vars и чеклист после deployment: [`PHASE2_RELEASE_NOTES.md`](PHASE2_RELEASE_NOTES.md).
