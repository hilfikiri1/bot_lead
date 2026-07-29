"""Deterministic router for B&BS operational Telegram commands."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoutedCommand:
    intent: str
    args: dict[str, Any] = field(default_factory=dict)


def _lead_id(text: str) -> int | None:
    match = re.search(r"(?:#|\b)(\d{4,12})\b", text)
    return int(match.group(1)) if match else None


def route(text: str) -> RoutedCommand | None:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return None

    if normalized in {"дайджест", "digest", "задачи дня", "что делать сегодня"}:
        return RoutedCommand("digest")
    if "проверь notion" in normalized or "тест notion" in normalized:
        return RoutedCommand("notion_test")
    if "синхрониз" in normalized and any(word in normalized for word in ("сделк", "лид", "kommo")):
        return RoutedCommand("sync_leads")
    if normalized in {"ошибки", "последние ошибки", "журнал ошибок"}:
        return RoutedCommand("errors")

    lead_id = _lead_id(normalized)
    if lead_id:
        if any(word in normalized for word in ("кп", "коммерческ")):
            return RoutedCommand("draft", {"kind": "commercial_offer", "lead_id": lead_id})
        if any(word in normalized for word in ("поставщик", "фабрик", "запрос производителю")):
            return RoutedCommand("draft", {"kind": "supplier_brief", "lead_id": lead_id})
        if "каталог" in normalized or "прайс" in normalized:
            return RoutedCommand("draft", {"kind": "catalog_outline", "lead_id": lead_id})
        if any(word in normalized for word in ("письмо", "follow-up", "фоллоу", "сообщение клиенту")):
            return RoutedCommand("draft", {"kind": "followup_message", "lead_id": lead_id})

    return None
