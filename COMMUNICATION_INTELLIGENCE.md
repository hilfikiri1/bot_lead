# B&BS Communication Intelligence v1

This module improves client-facing drafts without fine-tuning.

## Source priority

1. Latest real client conversation.
2. Confirmed Kommo facts.
3. Direct manager instruction.
4. Approved anonymized B&BS examples.
5. General B&BS communication playbook.

The writer receives up to 30 recent chat messages when Kommo external chat history is available. It also receives sent-message notes marked with `BBS-MSG-*`.

## Included in v1

- canonical B&BS communication playbook;
- anonymized few-shot examples derived from manager-approved conversations;
- full `chat_context` delivery to the writer;
- deterministic example retrieval by language, channel, intent and semantic token overlap;
- Writer → Reviewer pipeline;
- hard checks for placeholders, HTML entities, unsupported guarantees, excessive WhatsApp length and question overload;
- persistence of original, reviewed and manager-final message versions inside `client_message_drafts.metadata_json.communication_intelligence`;
- sent drafts marked as future approved examples.

No client phone numbers or email addresses are stored in the bundled examples.

## Runtime variables

```env
# Existing
OPENAI_API_KEY=...
AGENT_WRITER_MODEL=...
KOMMO_CHAT_CONTEXT_ENABLED=true

# New optional variables
AGENT_MESSAGE_REVIEWER_ENABLED=true
AGENT_REVIEWER_MODEL=
```

If `AGENT_REVIEWER_MODEL` is empty, the reviewer uses the planner model, then the writer model, then `OPENAI_MODEL`.

If the reviewer API call fails, the original draft remains available and deterministic checks are returned to the manager. Client messages are never sent automatically.

## Knowledge files

- `data/bbs_knowledge/communication_playbook.json`
- `data/bbs_knowledge/communication_examples.json`

The first file contains canonical rules. The second contains only anonymized examples and lessons. Live lead state remains in Kommo/PostgreSQL and is not copied into the static knowledge base.

## Next phase

A later migration can promote approved sent drafts from JSON metadata into a queryable `communication_examples` table and optionally mirror approved, anonymized examples into an OpenAI Vector Store. The v1 service interfaces are intentionally compatible with that extension.
