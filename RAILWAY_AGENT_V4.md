# Railway — Agent v4 Deployment

## 1. Database

Run migrations after deploy:

```bash
alembic upgrade head
```

After the identity/language/WhatsApp block, `alembic current` must show:

```text
009_agent_v4_identity
```

## 2. New environment variables

Add to the Railway service (see `.env.example`):

```
GOOGLE_DRIVE_ENABLED=true
GOOGLE_DRIVE_ROOT_FOLDER_ID=...
GOOGLE_DRIVE_PROJECTS_FOLDER_ID=...
AGENT_MORNING_DIGEST_ENABLED=true
AGENT_MORNING_DIGEST_HOUR=8
AGENT_EVENING_DIGEST_ENABLED=true
AGENT_EVENING_DIGEST_HOUR=19
AGENT_DIGEST_TIMEZONE=Europe/Warsaw
TELEGRAM_OWNER_USER_ID=...
TELEGRAM_BOT_USERNAME=...
AGENT_DEFAULT_CLIENT_LANGUAGE=pl
AGENT_INVITE_TTL_HOURS=48
```

Use the same `GOOGLE_SERVICE_ACCOUNT_JSON` (or `_BASE64`) as Calendar.

## 3. Drive permissions

Share Drive folders with the service account email before enabling writes.

## 4. Rollout order

1. Deploy with `GOOGLE_DRIVE_ENABLED=false` — read-only snapshot/digest/costs work
2. Verify `/costs` and `/digest`
3. Enable Drive and test one project creation on a staging lead
4. Enable morning/evening digests after manual `/digest` looks correct
5. Verify `/team`, create one `/invite`, bind the Manager to a Kommo User ID
6. Smoke-test a Polish follow-up, WhatsApp deep link, `.vcf` and Kommo sent note

## 5. Monitoring

- Check agent integration errors: `ошибки` or `/errors` in Telegram
- AI budget warnings appear in `/costs` when daily/monthly thresholds are set

Full checklist: [`AGENT_V4_IDENTITY_LANGUAGE_WHATSAPP.md`](AGENT_V4_IDENTITY_LANGUAGE_WHATSAPP.md).
