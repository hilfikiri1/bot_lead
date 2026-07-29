# B&BS Automatic Follow-up Engine

## Workflow

1. A manager opens the generated WhatsApp draft and sends it manually.
2. The manager confirms the send in Telegram.
3. Telegram asks when to check for a reply: tomorrow, in 3 days, in 7 days, a custom date, or no reminder.
4. The selected deadline is stored in `next_action_states` and a marked Kommo task is created.
5. The background loop checks due follow-ups every five minutes and sends a Telegram reminder.
6. The reminder can generate a new AI follow-up, snooze the check, mark that the client replied, or close the waiting state.
7. A verified incoming WhatsApp Cloud API message automatically closes an active waiting state and changes it to `waiting_on=us`.

## Safety

- Messages are never sent automatically.
- The existing Kommo/Telegram confirmation flow remains mandatory.
- Column W and Google Sheets are not modified by the follow-up engine.
- Deal stages are not changed automatically.
- A marker such as `[BBS-FOLLOWUP-17]` prevents duplicate Kommo tasks for the same sent message.
- Incoming messages older than the recorded outbound message do not close the waiting state.
- Reminder delivery is rate-limited to at most once per 20 hours until the manager acts.

## Runtime

```env
AGENT_FOLLOWUP_ENABLED=true
MANAGER_TIMEZONE=Europe/Warsaw
```

For automatic reply reconciliation, the WhatsApp Cloud API webhook must also be configured:

```env
WHATSAPP_VERIFY_TOKEN=...
WHATSAPP_APP_SECRET=...
```

Without WhatsApp API access the reminder engine still works. The manager can manually press `Клиент ответил` when a reply arrives through another channel.
