# B&BS WhatsApp Cloud API Inbox & Outbox

This module turns the existing Meta webhook into a durable, manually confirmed WhatsApp channel for the Telegram agent.

## What it does

- validates Meta webhook signatures with `WHATSAPP_APP_SECRET`;
- stores incoming WhatsApp events idempotently by Meta message ID;
- stores text, sender, timestamps, context IDs and media metadata;
- finds the existing Kommo deal by phone;
- writes incoming/outgoing messages to Kommo notes;
- closes an active follow-up when the client replies;
- shows the incoming message in Telegram;
- adds `📤 Отправить через WhatsApp API` to a prepared Telegram draft;
- sends only after a manager presses the button;
- stores the Meta message ID and status history;
- accepts `sent`, `delivered`, `read` and `failed` webhook statuses;
- prevents repeat sends of the same draft;
- blocks free-form text outside the 24-hour customer-service window.

The module does not automatically send client messages.

## Railway variables

```env
WHATSAPP_VERIFY_TOKEN=your-own-verification-string
WHATSAPP_APP_SECRET=meta-app-secret
WHATSAPP_ACCESS_TOKEN=permanent-system-user-token
WHATSAPP_PHONE_NUMBER_ID=registered-phone-number-id
WHATSAPP_GRAPH_API_VERSION=v23.0
WHATSAPP_ENFORCE_24H_WINDOW=true
```

Do not include quotes or spaces around secrets.

The access token must have the WhatsApp Business messaging permission for the selected business account and phone number.

## Meta webhook

Callback URL:

```text
https://botlead-production.up.railway.app/webhook/whatsapp
```

Subscribe the WhatsApp Business Account webhook to the `messages` field. The same field contains inbound messages and status callbacks.

## 24-hour safety

Free-form text can be sent only when the database contains an incoming message from the same phone in the previous 24 hours. If the window is closed, Telegram blocks the send and explains that an approved Meta template is required.

Template sending is intentionally left for the next module.

For controlled testing only, the check can be disabled temporarily:

```env
WHATSAPP_ENFORCE_24H_WINDOW=false
```

Do not keep this disabled in production.

## Database

Migration `013_whatsapp_cloud_messages` adds `whatsapp_cloud_messages` with:

- provider message ID;
- direction and status;
- phone and client name;
- Kommo lead ID;
- linked Telegram draft ID;
- text and media metadata;
- sent/delivered/read/failed timestamps;
- provider payload and error information.

## Deliberately deferred

A following PR should add:

- approved Meta template selection and variables;
- downloading and forwarding image/PDF/audio content;
- sending media;
- status display inside a conversation card;
- token health diagnostics and expiry warnings.
