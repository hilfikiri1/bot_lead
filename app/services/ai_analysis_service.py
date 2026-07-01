"""Structured AI analysis for B2B client conversations.

Manager-facing content is always Russian. Client-facing drafts use the client's
language (Polish by default for Polish leads).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_client = AsyncOpenAI(api_key=settings.openai_api_key)

SYSTEM_PROMPT = """You are a senior B2B sales analyst for Buy & Bring Solutions, a sourcing and logistics company importing business goods, machinery and materials from China to Poland, Ukraine and the EU.

You analyse a transcript of a manager-client conversation.

OUTPUT RULES:
- Return ONLY one valid JSON object. No markdown or preamble.
- All manager-facing fields MUST be written in natural, concise Russian.
- Client-facing drafts MUST use the client's language. For a Polish client, write the WhatsApp and email drafts in Polish.
- Never invent facts. Unknown values must be null, an empty list, or explicitly described as "не указано" only in Russian narrative fields.
- Keep facts separate from risks and missing information.
- Do not promise final prices, delivery dates, customs rates, certifications or supplier availability unless explicitly stated.
- For equipment, identify missing technical parameters such as capacity, power, dimensions, material, voltage, accuracy, certificates, quantity, delivery city, Incoterm, target budget and timing when relevant.
- The proposed Kommo lead title should be short and product-focused. Do not invent a lead number.
- The manager task should be one concrete action. Add an ISO-8601 due time only if the transcript clearly contains one.
- Confidence is 0.0-1.0. needs_human_review must be true when confidence is below 0.75 or critical information is missing.

JSON SCHEMA:
{
  "client": {
    "name": "string|null",
    "phone": "string|null",
    "email": "string|null",
    "company": "string|null",
    "language": "ru|pl|ua|uk|en|unknown"
  },
  "lead": {
    "lead_number": "string|null",
    "proposed_name": "string",
    "product_requested": "string",
    "specifications": ["string"],
    "quantity": "string|null",
    "budget": "string|null",
    "country": "string|null",
    "city": "string|null",
    "delivery_terms": "string|null",
    "certification": "string|null",
    "timeline": "string|null",
    "urgency": "low|medium|high|unknown",
    "status": "new|needs_info|ready_for_supplier_search|follow_up"
  },
  "conversation_summary": "Russian string",
  "confirmed_facts": ["Russian string"],
  "what_manager_said": ["Russian string"],
  "mistakes_or_weak_points": ["Russian string"],
  "missing_questions": ["Russian string"],
  "risks": ["Russian string"],
  "recommended_next_step": "Russian string",
  "manager_task": {
    "title": "Russian string|null",
    "due_at": "ISO-8601 string|null"
  },
  "email": {
    "subject": "client-language string",
    "body": "client-language string"
  },
  "whatsapp": {
    "message": "client-language string"
  },
  "calendar": {
    "title": "Russian string",
    "description": "Russian string",
    "start_time": "ISO-8601 string|null",
    "duration_minutes": 15
  },
  "confidence_score": 0.0,
  "needs_human_review": true
}
"""


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
async def analyse_transcript(transcript: str) -> dict[str, Any]:
    """Analyse a transcript and return a normalised structured report."""
    clean = transcript.strip()
    if not clean:
        raise ValueError("Транскрипт пустой.")

    logger.info("Sending transcript to AI analysis (%d chars)", len(clean))
    response = await _client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Analyse this conversation transcript. Remember: all manager-facing "
                    "analysis must be in Russian, while client drafts use the client's language.\n\n"
                    f"TRANSCRIPT:\n{clean}"
                ),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.15,
    )

    raw = response.choices[0].message.content or ""
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse AI JSON: %s", exc)
        raise ValueError(f"AI returned invalid JSON: {exc}") from exc

    result = _normalise_analysis(result)
    _validate_schema(result)
    logger.info(
        "AI analysis complete. confidence=%.2f", result.get("confidence_score", 0)
    )
    return result


def _normalise_analysis(data: dict[str, Any]) -> dict[str, Any]:
    """Add safe defaults while preserving backward compatibility."""
    client = data.setdefault("client", {})
    lead = data.setdefault("lead", {})
    data.setdefault("confirmed_facts", data.get("what_manager_said") or [])
    data.setdefault("what_manager_said", [])
    data.setdefault("mistakes_or_weak_points", [])
    data.setdefault("missing_questions", [])
    data.setdefault("risks", data.get("mistakes_or_weak_points") or [])
    data.setdefault("recommended_next_step", "Уточнить недостающие данные у клиента.")
    data.setdefault("manager_task", {"title": None, "due_at": None})
    if not isinstance(data.get("manager_task"), dict):
        data["manager_task"] = {"title": None, "due_at": None}
    data.setdefault("email", {"subject": "", "body": ""})
    if not isinstance(data.get("email"), dict):
        data["email"] = {"subject": "", "body": ""}
    data.setdefault("whatsapp", {"message": ""})
    if not isinstance(data.get("whatsapp"), dict):
        data["whatsapp"] = {"message": ""}
    data.setdefault(
        "calendar",
        {
            "title": "Повторный контакт с клиентом",
            "description": data.get("recommended_next_step") or "",
            "start_time": None,
            "duration_minutes": 15,
        },
    )
    if not isinstance(data.get("calendar"), dict):
        data["calendar"] = {
            "title": "Повторный контакт с клиентом",
            "description": data.get("recommended_next_step") or "",
            "start_time": None,
            "duration_minutes": 15,
        }
    data.setdefault("confidence_score", 0.0)
    data.setdefault("needs_human_review", True)

    client.setdefault("name", None)
    client.setdefault("phone", None)
    client.setdefault("email", None)
    client.setdefault("company", None)
    client.setdefault("language", "unknown")

    product = str(lead.get("product_requested") or "Новый запрос").strip()
    lead.setdefault("lead_number", None)
    lead.setdefault("proposed_name", product[:255])
    lead.setdefault("product_requested", product)
    lead.setdefault("specifications", [])
    lead.setdefault("quantity", None)
    lead.setdefault("budget", None)
    lead.setdefault("country", None)
    lead.setdefault("city", None)
    lead.setdefault("delivery_terms", None)
    lead.setdefault("certification", None)
    lead.setdefault("timeline", None)
    lead.setdefault("urgency", "unknown")
    lead.setdefault("status", "needs_info")
    return data


def _validate_schema(data: dict[str, Any]) -> None:
    required_top = {
        "client",
        "lead",
        "conversation_summary",
        "missing_questions",
        "recommended_next_step",
        "email",
        "whatsapp",
        "calendar",
        "confidence_score",
        "needs_human_review",
    }
    missing = required_top - set(data.keys())
    if missing:
        raise ValueError(f"AI response missing required keys: {missing}")
    if not isinstance(data.get("client"), dict) or not isinstance(
        data.get("lead"), dict
    ):
        raise ValueError("AI response client/lead must be objects")
    for key in ("email", "whatsapp", "calendar", "manager_task"):
        if not isinstance(data.get(key), dict):
            raise ValueError(f"AI response field {key} must be an object")
    for key in (
        "confirmed_facts",
        "what_manager_said",
        "mistakes_or_weak_points",
        "missing_questions",
        "risks",
    ):
        if not isinstance(data.get(key), list):
            raise ValueError(f"AI response field {key} must be a list")
    try:
        score = float(data.get("confidence_score", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("AI confidence_score must be numeric") from exc
    if not 0 <= score <= 1:
        raise ValueError("AI confidence_score must be between 0 and 1")
