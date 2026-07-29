"""Retrieve anonymized B&BS communication examples for few-shot drafting."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_PATH = _ROOT / "data" / "bbs_knowledge" / "communication_examples.json"
_TOKEN_RE = re.compile(r"[A-Za-zÀ-žА-Яа-яЁёІіЇїЄєҐґ0-9]{3,}", re.UNICODE)
_SENSITIVE_RE = re.compile(
    r"(?:\+?\d[\d\s().-]{7,}\d)|(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})",
    re.UNICODE,
)


def _tokens(value: Any) -> set[str]:
    return {match.group(0).casefold() for match in _TOKEN_RE.finditer(str(value or ""))}


def _safe_text(value: Any, *, limit: int = 4000) -> str:
    text = " ".join(str(value or "").split())
    text = _SENSITIVE_RE.sub("[REDACTED]", text)
    return text[:limit]


@lru_cache(maxsize=1)
def load_examples() -> list[dict[str, Any]]:
    try:
        data = json.loads(_EXAMPLES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("approved_reply")]


def _score(
    item: dict[str, Any],
    *,
    kind: str,
    language: str,
    channel: str,
    query_tokens: set[str],
) -> float:
    score = 0.0
    item_language = str(item.get("language") or "").casefold()
    item_channel = str(item.get("channel") or "").casefold()
    item_kind = str(item.get("kind") or item.get("intent") or "").casefold()
    if item_language == language.casefold():
        score += 4.0
    elif item_language and language != "auto":
        score -= 2.0
    if item_channel == channel.casefold():
        score += 2.0
    if item_kind == kind.casefold():
        score += 3.0
    elif kind == "followup_message" and item_kind in {"followup", "client_message"}:
        score += 1.5

    searchable = " ".join(
        str(item.get(key) or "")
        for key in (
            "situation",
            "client_message",
            "manager_correction",
            "approved_reply",
            "tags",
            "lessons",
            "product_category",
        )
    )
    item_tokens = _tokens(searchable)
    if query_tokens and item_tokens:
        overlap = len(query_tokens & item_tokens)
        score += min(6.0, float(overlap) * 0.8)
        score += 2.0 * overlap / max(1, len(query_tokens))
    score += float(item.get("quality_weight") or 0)
    return score


def find_similar_examples(
    *,
    kind: str,
    language: str,
    channel: str,
    query: str,
    limit: int = 4,
) -> list[dict[str, Any]]:
    query_tokens = _tokens(query)
    ranked = sorted(
        (
            (
                _score(
                    item,
                    kind=kind,
                    language=language,
                    channel=channel,
                    query_tokens=query_tokens,
                ),
                item,
            )
            for item in load_examples()
        ),
        key=lambda pair: (pair[0], str(pair[1].get("id") or "")),
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    for score, item in ranked:
        if score <= 0:
            continue
        result.append(
            {
                "id": str(item.get("id") or ""),
                "situation": _safe_text(item.get("situation"), limit=800),
                "client_message": _safe_text(item.get("client_message"), limit=1200),
                "manager_correction": _safe_text(item.get("manager_correction"), limit=1000),
                "approved_reply": _safe_text(item.get("approved_reply"), limit=3000),
                "lessons": [
                    _safe_text(value, limit=500)
                    for value in (item.get("lessons") or [])
                    if _safe_text(value, limit=500)
                ][:8],
                "language": item.get("language"),
                "channel": item.get("channel"),
                "kind": item.get("kind") or item.get("intent"),
                "score": round(score, 3),
            }
        )
        if len(result) >= max(1, min(int(limit), 5)):
            break
    return result


def example_search_query(
    *,
    lead: dict[str, Any],
    communication_context: dict[str, Any],
    manager_request: str,
) -> str:
    conversation = communication_context.get("conversation") or {}
    fields = lead.get("custom_fields") or {}
    return " ".join(
        str(value or "")
        for value in (
            manager_request,
            lead.get("name"),
            lead.get("status_name"),
            conversation.get("last_client_message"),
            conversation.get("last_manager_message"),
            json.dumps(fields, ensure_ascii=False, default=str)[:3000],
        )
    )
