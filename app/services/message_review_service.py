"""Second-pass reviewer for B&BS client-facing drafts."""
from __future__ import annotations

import html
import json
import os
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings

settings = get_settings()

_PLACEHOLDER_RE = re.compile(
    r"\[(?:twoje imię|imię|name|your name|company|firma|товар|имя)[^\]]*\]",
    re.IGNORECASE,
)
_EXACT_PROMISE_PATTERNS = (
    r"\bgwarantujemy\b",
    r"\bna pewno dostarczymy\b",
    r"\bodpowiadamy za termin fabryki\b",
    r"\bмы гарантируем товар\b",
    r"\bмы отвечаем за срок фабрики\b",
    r"\bточно доставим\b",
    r"\bми гарантуємо товар\b",
)
_GENERIC_OPENINGS = (
    "mam nadzieję, że wszystko u pani w porządku",
    "mam nadzieję, że wszystko u pana w porządku",
    "i hope this message finds you well",
)


def _enabled() -> bool:
    return os.getenv("AGENT_MESSAGE_REVIEWER_ENABLED", "true").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _reviewer_model() -> str:
    return (
        os.getenv("AGENT_REVIEWER_MODEL", "").strip()
        or settings.agent_planner_model.strip()
        or settings.agent_writer_model.strip()
        or settings.openai_model
    )


def deterministic_review(
    *,
    body: str,
    kind: str,
    language: str,
    playbook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean = html.unescape(str(body or "").strip())
    issues: list[str] = []
    if not clean:
        issues.append("Черновик пустой.")
    if _PLACEHOLDER_RE.search(clean):
        issues.append("В тексте остался незаполненный шаблон или имя отправителя.")
    if "&amp;" in str(body or ""):
        issues.append("В тексте осталась HTML-сущность &amp;.")
    lowered = clean.casefold()
    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _EXACT_PROMISE_PATTERNS):
        issues.append("Текст содержит чрезмерную гарантию или обещание за фабрику.")
    if kind == "followup_message":
        question_count = clean.count("?") + clean.count("？")
        if question_count > 3:
            issues.append("Для follow-up задано слишком много вопросов.")
        if len(clean) > 1800:
            issues.append("Follow-up слишком длинный для WhatsApp.")
    if kind == "email" and len(clean) > 7000:
        issues.append("Email слишком длинный.")
    if any(opening in lowered for opening in _GENERIC_OPENINGS):
        issues.append("Начало звучит шаблонно и не продолжает конкретный разговор.")
    if language == "pl" and re.search(r"\b(szanowny panie|szanowna pani)\b", lowered):
        if kind == "followup_message":
            issues.append("Для WhatsApp обращение может быть излишне формальным.")

    rules = (playbook or {}).get("reviewer_checks") or []
    return {
        "approved": not issues,
        "issues": issues,
        "corrected_body": clean,
        "model": "deterministic",
        "checks_applied": [str(rule) for rule in rules[:20]],
    }


async def review_draft(
    *,
    body: str,
    kind: str,
    language: str,
    communication_context: dict[str, Any],
    playbook: dict[str, Any],
) -> dict[str, Any]:
    baseline = deterministic_review(
        body=body,
        kind=kind,
        language=language,
        playbook=playbook,
    )
    if not _enabled() or not settings.openai_api_key.strip():
        return baseline

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    model = _reviewer_model()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are the quality reviewer for Buy & Bring Solutions client communications. "
                        "Do not rewrite from scratch unless necessary. Check whether the draft continues "
                        "the latest client conversation, uses only confirmed facts, avoids repeated questions, "
                        "matches the requested language and channel, contains no placeholders, and ends with "
                        "one practical next step. Never add prices, availability, certificates, deadlines or "
                        "guarantees that are not present in the supplied context. Return JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "draft_kind": kind,
                            "language": language,
                            "draft": body,
                            "deterministic_issues": baseline["issues"],
                            "communication_context": communication_context,
                            "bbs_rules": playbook,
                            "schema": {
                                "approved": "boolean",
                                "issues": ["string"],
                                "corrected_body": "string",
                            },
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.05,
        )
        data = json.loads(response.choices[0].message.content or "{}")
        corrected = html.unescape(str(data.get("corrected_body") or body).strip())
        issues = [
            str(value).strip()
            for value in (data.get("issues") or [])
            if str(value).strip()
        ][:20]
        final_checks = deterministic_review(
            body=corrected,
            kind=kind,
            language=language,
            playbook=playbook,
        )
        merged_issues = list(dict.fromkeys(issues + final_checks["issues"]))
        return {
            "approved": bool(data.get("approved")) and not merged_issues,
            "issues": merged_issues,
            "corrected_body": corrected,
            "model": model,
            "checks_applied": baseline.get("checks_applied") or [],
        }
    except Exception as exc:
        baseline["issues"] = list(
            dict.fromkeys(
                baseline["issues"]
                + [f"AI Reviewer временно недоступен: {type(exc).__name__}."]
            )
        )
        baseline["approved"] = False if baseline["issues"] else True
        baseline["model"] = f"{model}:fallback"
        return baseline
