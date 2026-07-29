"""Document classification helpers and content-hash dedupe for Agent v5."""

from __future__ import annotations

import hashlib
import re
from typing import Any


DOCUMENT_CLASSES = {
    "technical_brief": "техническое задание",
    "commercial_offer": "коммерческое предложение",
    "price_list": "прайс",
    "catalog": "каталог",
    "invoice": "счёт",
    "packing_list": "packing list",
    "contract": "договор",
    "certificate": "сертификат",
    "specification": "спецификация",
    "product_photo": "фотография товара",
    "correspondence": "переписка",
    "calculation": "расчёт",
    "logistics": "логистика",
    "customs": "таможенные документы",
    "audio": "аудио переговоров",
    "other": "другое",
}

_CLASS_HINTS = {
    "technical_brief": ("тз", "tech", "spec", "brief", "requirement"),
    "commercial_offer": ("offer", "кп", "proposal", "quotation", "quote"),
    "price_list": ("price", "прайс", "pricelist"),
    "catalog": ("catalog", "каталог"),
    "invoice": ("invoice", "счёт", "счет", "inv"),
    "packing_list": ("packing", "pack list"),
    "contract": ("contract", "договор", "agreement"),
    "certificate": ("cert", "сертиф", "iso", "ce "),
    "specification": ("datasheet", "spec sheet"),
    "product_photo": ("photo", "img", "image", "jpg", "png"),
    "correspondence": ("mail", "whatsapp", "chat", "переписк"),
    "calculation": ("calc", "расчёт", "расчет", "xlsx"),
    "logistics": ("logist", "shipping", "bl", "awb"),
    "customs": ("customs", "тамож", "hs code"),
    "audio": ("ogg", "mp3", "wav", "m4a", "voice"),
}


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content or b"").hexdigest()


def classify_document(*, filename: str, mime_type: str = "", caption: str | None = None) -> str:
    blob = f"{filename} {mime_type} {caption or ''}".casefold()
    if mime_type.startswith("audio/") or filename.casefold().endswith((".ogg", ".mp3", ".wav", ".m4a")):
        return "audio"
    if mime_type.startswith("image/"):
        return "product_photo"
    for class_name, hints in _CLASS_HINTS.items():
        if any(hint in blob for hint in hints):
            return class_name
    return "other"


def suggested_drive_subfolder(document_class: str) -> str:
    mapping = {
        "technical_brief": "02 Техническое задание",
        "commercial_offer": "07 Коммерческие предложения",
        "price_list": "04 Прайсы фабрик",
        "catalog": "04 Прайсы фабрик",
        "invoice": "10 Договоры, инвойсы и оплата",
        "contract": "10 Договоры, инвойсы и оплата",
        "certificate": "08 Сертификаты и проверка",
        "product_photo": "05 Фото, видео и образцы",
        "calculation": "06 Расчёты и сравнение",
        "logistics": "09 Логистика и таможня",
        "customs": "09 Логистика и таможня",
        "audio": "01 Запрос клиента",
    }
    return mapping.get(document_class, "01 Запрос клиента")


def extract_document_fields(text: str) -> dict[str, Any]:
    """Heuristic extraction — facts only from explicit patterns, no invented values."""
    data: dict[str, Any] = {}
    conflicts: list[str] = []
    if not text:
        return {"fields": data, "conflicts": conflicts, "confidence": 0.0}

    currency_matches = re.findall(r"(?:USD|EUR|PLN|CNY|\$|€)\s?[\d\s]{2,}", text, flags=re.I)
    if currency_matches:
        unique = list(dict.fromkeys(m.strip() for m in currency_matches))
        data["prices"] = unique[:8]
        if len(unique) > 1:
            conflicts.append("В документе несколько денежных сумм")

    moq = re.search(r"\bMOQ\s*[:=]?\s*(\d[\d\s]*)", text, flags=re.I)
    if moq:
        data["moq"] = moq.group(1).replace(" ", "")

    incoterms = re.search(r"\b(FOB|CIF|EXW|DDP|DAP|CFR)\b", text, flags=re.I)
    if incoterms:
        data["incoterms"] = incoterms.group(1).upper()

    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, flags=re.I)
    if emails:
        data["emails"] = list(dict.fromkeys(emails))[:5]

    return {
        "fields": data,
        "conflicts": conflicts,
        "confidence": 0.55 if data else 0.1,
        "assumptions": [],
    }
