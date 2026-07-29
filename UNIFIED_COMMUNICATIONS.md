# B&BS Unified Communications

The Telegram lead card exposes `💬 Вся переписка` and builds one chronological view from the communication sources currently available to the agent.

## Sources

- Kommo external chats such as Facebook, WhatsApp and Instagram;
- confirmed sent client-message drafts;
- incoming WhatsApp Cloud API notes;
- common Kommo notes;
- Agent v5 project events for calls, email, voice notes, conversations and promises.

The service continues with partial data when one source is unavailable. In particular, missing `External chat history` permission does not block notes, drafts or project events.

## Analysis

The timeline calculates:

- last contact time and channel;
- who wrote last and who should act now;
- latest client and manager messages;
- client requests and open questions;
- manager promises;
- explicit and relative promise deadlines;
- overdue promises;
- a recommended next action.

The analysis is read-only. It does not change a Kommo stage, marketing status or client message automatically.

## Merge order

This change is stacked on the automatic follow-up engine and should be merged after PR #50. After PR #50 reaches `main`, retarget this PR to `main` and re-run CI before merge.
