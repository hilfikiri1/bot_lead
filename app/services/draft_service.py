"""AI-generated working drafts based on a confirmed Kommo lead card.

Drafts are never sent automatically. They are returned to Telegram and stored
in Notion for manager review.
"""
from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

settings = get_settings()

KIND_INSTRUCTIONS = {
    "commercial_offer": (
        "Prepare a concise commercial proposal draft in Russian for internal "
        "review. Include assumptions, scope, commercial-term placeholders and "
        "missing data. Never invent prices."
    ),
    "supplier_brief": (
        "Prepare a precise supplier/factory inquiry in English. Include all "
        "available technical requirements and a separate list of missing "
        "questions. Never invent specifications."
    ),
    "catalog_outline": (
        "Prepare a Russian outline for a client-facing catalog or price list: "
        "sections, product table columns, required images/documents and missing "
        "source data."
    ),
    "followup_message": (
        "Prepare a short client follow-up message. Use the likely client "
        "language when it is present in the lead data; otherwise use Russian. "
        "Do not promise price or delivery."
    ),
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def generate(kind: str, lead: dict[str, Any]) -> dict[str, Any]:
    instruction = KIND_INSTRUCTIONS.get(kind)
    if not instruction:
        raise ValueError(f"Unsupported draft kind: {kind}")
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    prompt = {
        "instruction": instruction,
        "lead": lead,
        "rules": [
            "Return only JSON.",
            "Do not invent facts.",
            "Put unknown critical inputs into missing_data.",
            "Keep the draft practical and ready for manager review.",
        ],
        "schema": {
            "title": "string",
            "body": "string",
            "missing_data": ["string"],
            "next_action": "string",
            "language": "string",
        },
    }
    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the operational B2B sourcing assistant of Buy & "
                    "Bring Solutions. You create drafts, never final commitments."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False, default=str),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.15,
    )
    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    if not isinstance(data.get("body"), str) or not data["body"].strip():
        raise ValueError("AI returned an empty draft")
    data.setdefault("title", "Рабочий черновик")
    data.setdefault("missing_data", [])
    data.setdefault("next_action", "Проверить и дополнить черновик")
    data.setdefault("language", "ru")
    return data
