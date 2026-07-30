"""Short Russian product titles for Kommo lead names."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_client = AsyncOpenAI(api_key=settings.openai_api_key)

_TITLE_CACHE: dict[str, str] = {}
_MAX_CHARS = 35

_DETERMINISTIC_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bherbat", re.I), "Чай"),
    (re.compile(r"zabawk", re.I), "Игрушки"),
    (re.compile(r"minikopark", re.I), "Мини-экскаваторы"),
    (re.compile(r"pellet.*sosn|sosnowy.*pellet", re.I), "Сосновый пеллет"),
    (re.compile(r"p[eę]dzle.*makija", re.I), "Кисти для макияжа"),
    (re.compile(r"w[lł]os[yw].*natural", re.I), "Натуральные волосы"),
    (re.compile(r"\bdron", re.I), "Дроны"),
    (re.compile(r"klejenia drewna|tartaczn|drewn", re.I), "Деревообработка"),
    (re.compile(r"tamborki|hafciark", re.I), "Пяльцы для вышивки"),
    (re.compile(r"\bnarz[eę]dzi", re.I), "Инструменты"),
    (re.compile(r"fotele?\s+autobusow", re.I), "Автобусные сиденья"),
    (re.compile(r"pokry(?:cia|wa)\s+pod[lł]ogow", re.I), "Напольные покрытия"),
    (
        re.compile(r"karmnik\w*\s+i\s+poid[lł]\w*|poid[lł]\w*\s+i\s+karmnik\w*", re.I),
        "Кормушки и поилки для птицы",
    ),
    (
        re.compile(
            r"artyku[lł]y?\s+elektrycz|elektro\s*towar|electrical\s+article|"
            r"electrical\s+goods|электротовар",
            re.I,
        ),
        "Электротовары",
    ),
]


def _cache_key(product: str) -> str:
    return _fold(product)


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def _validate_title(title: str) -> str:
    clean = " ".join((title or "").split()).strip()
    if not clean:
        raise ValueError("Пустое название товара.")
    if len(clean) > _MAX_CHARS:
        clean = clean[:_MAX_CHARS].rstrip(" -,")
    if re.search(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", clean):
        raise ValueError("Название должно быть на русском.")
    words = clean.split()
    if len(words) > 4:
        clean = " ".join(words[:3])
    return clean


def _safe_fallback_title(product: str) -> str:
    """Never raise — used when AI/rules cannot produce a Russian title."""
    source = re.sub(r"\s+", " ", (product or "").strip())
    # Keep only Cyrillic words if the source already mixes languages.
    cyrillic_words = re.findall(r"[А-Яа-яЁё][А-Яа-яЁё\-]*", source)
    if cyrillic_words:
        candidate = " ".join(cyrillic_words[:3])[:_MAX_CHARS].strip()
        if candidate:
            return candidate
    return "Товар"


def deterministic_short_title(product: str | None) -> str | None:
    folded = _fold(product or "")
    if not folded:
        return None
    for pattern, title in _DETERMINISTIC_RULES:
        if pattern.search(folded):
            return title
    return None


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=4))
async def _ai_short_title(product: str) -> str:
    if not settings.openai_api_key.strip():
        raise ValueError("OPENAI_API_KEY не задан для перевода товара.")

    response = await _client.chat.completions.create(
        model=settings.openai_model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "Convert a B2B product request into one or two clear Russian words "
                    "for a CRM lead title. Return ONLY JSON: "
                    '{"short_product_ru": "..."}. '
                    "Rules: Russian only, max 35 chars, preserve product category, "
                    "remove quantities and marketing noise, do not invent another product."
                ),
            },
            {"role": "user", "content": product},
        ],
    )
    raw = (response.choices[0].message.content or "").strip()
    try:
        payload: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Некорректный ответ OpenAI.") from exc
    title = str(payload.get("short_product_ru") or "").strip()
    return _validate_title(title)


async def short_product_title(product: str | None) -> str:
    source = (product or "").strip()
    if not source:
        return "Товар"

    key = _cache_key(source)
    cached = _TITLE_CACHE.get(key)
    if cached:
        return cached

    mapped = deterministic_short_title(source)
    if mapped:
        _TITLE_CACHE[key] = mapped
        return mapped

    title: str | None = None
    try:
        title = await _ai_short_title(source)
    except Exception as exc:
        logger.warning("AI product title fallback failed: %s", exc)

    if title:
        try:
            validated = _validate_title(title)
            _TITLE_CACHE[key] = validated
            return validated
        except ValueError as exc:
            logger.warning("AI product title rejected (%s): %s", exc, title[:80])

    validated = _safe_fallback_title(source)
    _TITLE_CACHE[key] = validated
    return validated


def clear_title_cache() -> None:
    _TITLE_CACHE.clear()
