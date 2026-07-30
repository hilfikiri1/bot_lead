"""Structured OpenAI qualification call with strict validation and repair.

Reuses the same ``AsyncOpenAI`` client construction pattern already used by
``app.services.ai_analysis_service`` and ``app.services.product_title_service``
instead of creating another OpenAI client.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.config import get_settings
from app.services.lead_intake.prompt import REPAIR_INSTRUCTIONS, SYSTEM_PROMPT
from app.services.lead_intake.schema import (
    LeadQualification,
    LeadQualificationError,
    build_response_schema,
)

logger = logging.getLogger(__name__)
settings = get_settings()
_client = AsyncOpenAI(api_key=settings.openai_api_key)

_RESPONSE_SCHEMA_NAME = "lead_qualification_v1"


def _user_message(payload: dict[str, Any]) -> str:
    return (
        "Qualify this B2B lead using only the data below. Unknown fields are "
        "already represented as null — do not fill them with a guess.\n\n"
        f"LEAD DATA (JSON):\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


async def _call_openai(messages: list[dict[str, str]]) -> dict[str, Any]:
    if not settings.openai_api_key.strip():
        raise LeadQualificationError("OPENAI_API_KEY не задан.")

    try:
        response = await _client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.2,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": _RESPONSE_SCHEMA_NAME,
                    "schema": build_response_schema(),
                    "strict": True,
                },
            },
        )
    except Exception as exc:
        logger.info(
            "OpenAI Structured Outputs unavailable (%s), falling back to json_object.",
            type(exc).__name__,
        )
        response = await _client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.2,
            messages=messages,
            response_format={"type": "json_object"},
        )

    raw = (response.choices[0].message.content or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LeadQualificationError(f"AI returned invalid JSON: {exc}") from exc


async def generate_lead_qualification(payload: dict[str, Any]) -> LeadQualification:
    """Generate and strictly validate a lead qualification, with one repair attempt.

    Per the lead-intake contract: never write an invalid AI response to
    Kommo. If the first response fails Pydantic validation we ask the model
    once, explicitly, to repair it; if that also fails we raise
    ``LeadQualificationError`` and the caller must not proceed to Apply.

    Deliberately not decorated with an automatic multi-attempt retry: the
    Telegram "🔄 Retry" button and the checkpointed apply saga already give
    the manager an explicit, visible way to re-run this step, and blindly
    retrying a deterministic validation failure would only burn OpenAI
    quota without changing the outcome.
    """
    base_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_message(payload)},
    ]
    raw = await _call_openai(base_messages)
    try:
        return LeadQualification.model_validate(raw)
    except ValidationError as exc:
        logger.warning("AI lead qualification failed schema validation: %s", exc)
        repair_messages = base_messages + [
            {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
            {
                "role": "user",
                "content": f"{REPAIR_INSTRUCTIONS}\n\nValidation errors:\n{exc}",
            },
        ]
        repaired_raw = await _call_openai(repair_messages)
        try:
            return LeadQualification.model_validate(repaired_raw)
        except ValidationError as exc2:
            logger.error("AI lead qualification repair also failed: %s", exc2)
            raise LeadQualificationError(
                f"AI response failed validation twice: {exc2}"
            ) from exc2
