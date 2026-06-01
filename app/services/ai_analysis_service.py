"""
ai_analysis_service.py
Sends the transcript to GPT-4o with a strict JSON schema and business rules
for Buy & Bring Solutions.
"""
from __future__ import annotations

import json
import logging
from tenacity import retry, stop_after_attempt, wait_exponential

from openai import AsyncOpenAI
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_client = AsyncOpenAI(api_key=settings.openai_api_key)

SYSTEM_PROMPT = """You are an expert B2B sales analyst for Buy & Bring Solutions — a sourcing and logistics company
that helps clients import goods (equipment, machinery, materials, business goods) from China to Poland, Ukraine, and the EU.

Your job is to analyse a manager's voice note (transcribed) recorded after a client call.

OUTPUT: Respond ONLY with a valid JSON object matching the schema below. No markdown, no preamble.

STRICT RULES:
1. NEVER invent missing facts. Use null for unknown values.
2. Separate FACTS (explicitly stated) from ASSUMPTIONS (inferred) — label assumptions with "[assumption]".
3. If price, quantity, delivery terms, product model, delivery city, or timeline are unclear → mark as missing.
4. Always add missing questions for: photo/video/spec/link when product description is vague.
5. For equipment: ask about capacity, power, dimensions, accuracy, material, voltage, certificates, delivery terms, target budget.
6. For China imports: mention Incoterms (EXW/FOB/CIF/DDP) only when useful; explain if needed.
7. Client language for drafts: Polish for Polish clients, Ukrainian for Ukrainian, Russian when requested, otherwise English.
8. Email: professional, concise. WhatsApp: friendly, natural, human — NOT mechanical.
9. Calendar description MUST include exact talking points for the next call.
10. Do NOT promise final price, delivery date, customs rate, or supplier availability unless explicitly stated.
11. WhatsApp messages must sound like a real person wrote them, not a bot.
12. Confidence score: 0.0–1.0 based on how much info is available.
13. needs_human_review = true if confidence < 0.7 OR any critical field is missing.

JSON SCHEMA:
{
  "client": {
    "name": "string|null",
    "phone": "string|null",
    "email": "string|null",
    "company": "string|null",
    "language": "ru|pl|ua|en|unknown"
  },
  "lead": {
    "product_requested": "string",
    "budget": "string|null",
    "country": "string|null",
    "city": "string|null",
    "urgency": "low|medium|high|unknown",
    "status": "new|needs_info|ready_for_supplier_search|follow_up"
  },
  "conversation_summary": "string",
  "what_manager_said": ["string"],
  "mistakes_or_weak_points": ["string"],
  "missing_questions": ["string"],
  "recommended_next_step": "string",
  "email": {
    "subject": "string",
    "body": "string"
  },
  "whatsapp": {
    "message": "string"
  },
  "calendar": {
    "title": "string",
    "description": "string",
    "start_time": "ISO-8601 string|null",
    "duration_minutes": 15
  },
  "confidence_score": 0.0,
  "needs_human_review": true
}"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
async def analyse_transcript(transcript: str) -> dict:
    """
    Send a transcript to GPT-4o for structured analysis.
    Returns the parsed JSON dict.
    """
    logger.info("Sending transcript to AI analysis (%d chars)", len(transcript))

    response = await _client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Here is the manager's voice note transcript. Analyse it and return the JSON report:\n\n"
                    f"{transcript}"
                ),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = response.choices[0].message.content
    logger.debug("Raw AI response: %s", raw[:500])

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse AI JSON: %s", e)
        raise ValueError(f"AI returned invalid JSON: {e}") from e

    _validate_schema(result)
    logger.info("AI analysis complete. confidence=%.2f", result.get("confidence_score", 0))
    return result


def _validate_schema(data: dict) -> None:
    """Light validation to catch obvious schema problems early."""
    required_top = {"client", "lead", "conversation_summary", "email", "whatsapp", "calendar"}
    missing = required_top - set(data.keys())
    if missing:
        raise ValueError(f"AI response missing required keys: {missing}")
