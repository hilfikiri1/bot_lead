# Agent v4 — Identity, Client Language and WhatsApp Handoff

This release adds the multi-user and client-communication foundation to the
existing B&BS Telegram agent. It does not enable automatic WhatsApp Cloud API
sending.

## Delivered behavior

- Agent users: `Owner`, `Admin`, `Manager`, `Viewer`.
- One-time `/invite` deep links with expiry and hashed tokens.
- `/team` overview and `/bind_kommo TELEGRAM_ID KOMMO_USER_ID`.
- Existing Railway allowlist remains a bootstrap/emergency access path.
- A Manager invited through the bot has `assigned` access and sees only Kommo
  leads whose `responsible_user_id` matches the bound Kommo User ID.
- Per-client `communication_language`: `pl`, `uk`, `ru`, `en`, `de`.
- Client-facing language priority:
  1. explicit language in the manager request;
  2. saved client preference / Kommo language field;
  3. previous correspondence;
  4. Poland/Ukraine market fallback;
  5. `AGENT_DEFAULT_CLIENT_LANGUAGE` (default `pl`).
- Follow-up drafts get WhatsApp, edit, language, vCard, sent and cancel controls.
- Confirming a manual send writes an audited Kommo note.
- A unique delivery marker prevents duplicate Kommo notes after a timeout/retry.

## Database migration

```bash
alembic upgrade head
alembic current
```

Expected head:

```text
009_agent_v4_identity
```

New tables:

- `agent_users`
- `agent_invites`
- `client_message_drafts`

The migration also adds communication-language fields to `clients` and
approver/executor fields to `pending_agent_actions`.

## Railway variables

```env
# Keep the existing allowlist during the first rollout.
ALLOWED_TELEGRAM_USER_IDS=OWNER_TELEGRAM_ID
# The rollout checklist name is supported as an alias:
TELEGRAM_ALLOWED_USER_IDS=OWNER_TELEGRAM_ID

TELEGRAM_OWNER_USER_ID=OWNER_TELEGRAM_ID
TELEGRAM_BOT_USERNAME=your_bot_username

AGENT_DEFAULT_CLIENT_LANGUAGE=pl
AGENT_INVITE_TTL_HOURS=48
```

`TELEGRAM_BOT_USERNAME` may be empty; the bot then calls Telegram `getMe` when
creating an invite.

Keep these values disabled for this manual WhatsApp release:

```env
WHATSAPP_ENABLED=false
```

`WHATSAPP_PHONE_NUMBER_ID` and `WHATSAPP_ACCESS_TOKEN` are not required for
Click to Chat.

## First owner bootstrap

On the first request after migration:

1. `TELEGRAM_OWNER_USER_ID` becomes `Owner`.
2. If it is empty, the first ID in the merged Telegram allowlist becomes
   `Owner`.
3. Other pre-existing allowlisted users become `Manager` with historical
   all-lead access. Their scope can be tightened later.

Do not remove the allowlist until the owner account appears in `/team`.

## Invite and bind a manager

1. Owner sends `/invite`.
2. Select `Менеджер`.
3. Forward the one-time link.
4. The employee opens it and presses Start.
5. Owner receives a connection notification.
6. Owner runs:

```text
/bind_kommo EMPLOYEE_TELEGRAM_ID EMPLOYEE_KOMMO_USER_ID
```

Until step 6, the Manager account is active but cannot open client deals. This
is intentional: the bot never guesses employee identity or lead ownership.

## WhatsApp smoke test

1. Open a Polish Kommo lead with a `+48` phone.
2. Ask: `подготовь follow-up по этой сделке`.
3. Verify the header says `WhatsApp — язык: PL`.
4. Press `Открыть WhatsApp`; verify the number and prefilled Polish message.
5. Press `Контакт .vcf`; import the contact on iPhone.
6. Change `PL → UA → PL`; verify the saved client language follows the button.
7. Press `Изменить текст`, submit a revised message and verify the deep link.
8. Send it manually in WhatsApp.
9. Press `Да, отметить в Kommo`.
10. Verify one Kommo note containing `[BBS-MSG-...]`, preparer, sender,
    language and message text.
11. Press the old confirmation again and verify no duplicate note is created.

## Security properties

- Invitation tokens are stored only as SHA-256 hashes.
- Invitations are one-time and expire.
- `Viewer` cannot draft, edit, confirm or execute writes.
- Managers with assigned scope cannot read another employee's lead.
- WhatsApp is never sent by the server in this release.
- Confirmation and delivery actors are stored separately in PostgreSQL.
