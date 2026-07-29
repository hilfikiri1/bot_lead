from __future__ import annotations

import re
from typing import Any

_SECRET_WORDS = ("token", "secret", "password", "authorization", "api_key", "cookie")


def sanitize_text(value: str | None, *, limit: int = 20_000) -> str | None:
    if value is None:
        return None
    text = str(value)[:limit]
    text = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=***",
        text,
    )
    return text


def sanitize_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "…"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if any(word in str(key).casefold() for word in _SECRET_WORDS):
                result[str(key)] = "***"
            else:
                result[str(key)] = sanitize_value(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [sanitize_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, tuple):
        return [sanitize_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        clean = sanitize_text(value) or ""
        return clean + ("…" if len(value) > 20_000 else "")
    return value
