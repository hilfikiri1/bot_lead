"""Compact first-contact briefing for newly onboarded advertising leads.

One OpenAI call returns both the short Russian product title and the
manager-facing talk points, so onboarding stays roughly as fast as the old
title-only call while producing a useful Kommo note.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings
from app.services import product_title_service

logger = logging.getLogger(__name__)
settings = get_settings()
_client = AsyncOpenAI(api_key=settings.openai_api_key)

_BRIEFING_CACHE: dict[str, "OnboardingBriefing"] = {}


@dataclass(frozen=True)
class OnboardingBriefing:
    short_product_ru: str
    about_ru: str
    talk_points_ru: list[str] = field(default_factory=list)
    call_goal_ru: str = ""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _cache_key(product: str, budget: str, channel: str, region: str) -> str:
    return "|".join(
        product_title_service._fold(item) for item in (product, budget, channel, region)
    )


def _channel_hint(channel: str) -> str:
    folded = channel.casefold()
    if any(token in folded for token in ("whats", "wa", "ватсап", "whatsapp")):
        return "whatsapp"
    if any(token in folded for token in ("mail", "email", "e-mail", "почт")):
        return "email"
    if any(
        token in folded
        for token in ("telefon", "phone", "połączenie", "polaczenie", "звон", "call")
    ):
        return "phone"
    return "unknown"


def build_heuristic_briefing(
    *,
    product: str | None,
    product_ru: str | None = None,
    budget: str | None = None,
    channel: str | None = None,
    region: str | None = None,
    client_name: str | None = None,
) -> OnboardingBriefing:
    """Deterministic briefing used when OpenAI is unavailable or fails."""
    raw_product = _clean(product) or "товар/оборудование"
    title = _clean(product_ru) or product_title_service.deterministic_short_title(raw_product)
    if not title:
        title = product_title_service._safe_fallback_title(raw_product)

    budget_text = _clean(budget)
    region_text = _clean(region) or "не указан"
    channel_kind = _channel_hint(_clean(channel))
    client = _clean(client_name)
    client_phrase = f"у {client}" if client else "у клиента"

    about_parts = [
        f"Новая рекламная заявка: клиент интересуется «{raw_product}» "
        f"(кратко: {title})."
    ]
    if budget_text:
        about_parts.append(f"В форме указан бюджет: {budget_text}.")
    else:
        about_parts.append("Бюджет в заявке не указан — нужно уточнить на первом контакте.")
    about_parts.append(f"Регион: {region_text}.")
    if channel_kind == "phone":
        about_parts.append("Клиент выбрал телефонный контакт — готовим короткий квалификационный звонок.")
    elif channel_kind == "whatsapp":
        about_parts.append("Клиент выбрал WhatsApp — можно писать, но звонок тоже допустим при наличии номера.")
    elif channel_kind == "email":
        about_parts.append("Клиент выбрал email — сначала уточнить детали письмом или подтвердить телефон.")

    talk_points = [
        f"Уточнить {client_phrase}, какой именно товар/оборудование нужен в категории «{title}» "
        "(модель, материал, размеры, фото или ТЗ).",
        "Спросить объём первой партии и желаемые сроки поставки.",
        "Выяснить, это разовая закупка или регулярные поставки.",
        "Уточнить целевую цену / бюджет и есть ли уже поставщик или только поиск.",
        "Спросить про сертификацию, бренд, упаковку и условия Incoterms/доставки в Польшу/ЕС.",
        "Зафиксировать, кто принимает решение и когда удобно продолжить разговор.",
    ]
    if budget_text and any(token in budget_text for token in ("20_000", "20000", "powyżej", "выше")):
        talk_points.insert(
            1,
            "Бюджет выглядит существенным — быстро проверить серьёзность запроса и готовность к образцу/предоплате.",
        )

    if channel_kind == "phone":
        goal = (
            f"За 5–7 минут понять конкретный запрос по «{title}», объём, бюджет и "
            "следующий шаг (ТЗ / фото / коммерческое предложение)."
        )
    elif channel_kind == "whatsapp":
        goal = (
            f"Получить от клиента конкретное ТЗ или фото по «{title}» и подтвердить "
            "объём/бюджет перед поиском фабрики."
        )
    else:
        goal = (
            f"Квалифицировать заявку по «{title}»: что именно нужно, объём, бюджет, "
            "сроки и готовность работать через байера."
        )

    return OnboardingBriefing(
        short_product_ru=title[:50],
        about_ru=" ".join(about_parts),
        talk_points_ru=talk_points[:8],
        call_goal_ru=goal,
    )


async def _ai_briefing(payload: dict[str, Any]) -> OnboardingBriefing:
    if not settings.openai_api_key.strip():
        raise ValueError("OPENAI_API_KEY не задан.")

    response = await _client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You prepare a first-contact briefing for a Buy & Bring Solutions "
                    "manager who sources products from China for B2B clients in Poland/EU. "
                    "Use ONLY the provided lead fields. Never invent factories, prices or "
                    "specs. Return ONLY JSON with keys: "
                    "short_product_ru (1-3 Russian words, max 35 chars), "
                    "about_ru (2-4 Russian sentences: what the lead wants and commercial context), "
                    "talk_points_ru (array of 5-8 concrete Russian talking points / questions "
                    "for the first call or WhatsApp), "
                    "call_goal_ru (one Russian sentence: the objective of the first contact). "
                    "Do not write raw JSON into about_ru. Be specific to the product category."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
    )
    raw = (response.choices[0].message.content or "").strip()
    data = json.loads(raw)
    title = product_title_service._validate_title(str(data.get("short_product_ru") or ""))
    about = _clean(data.get("about_ru"))
    goal = _clean(data.get("call_goal_ru"))
    points_raw = data.get("talk_points_ru") or []
    points = [_clean(item) for item in points_raw if _clean(item)]
    if not about or not points:
        raise ValueError("AI briefing missing about_ru/talk_points_ru.")
    return OnboardingBriefing(
        short_product_ru=title[:50],
        about_ru=about,
        talk_points_ru=points[:8],
        call_goal_ru=goal or "Квалифицировать запрос и зафиксировать следующий шаг.",
    )


async def build_onboarding_briefing(
    *,
    product: str | None,
    budget: str | None = None,
    channel: str | None = None,
    region: str | None = None,
    client_name: str | None = None,
    lead_status: str | None = None,
) -> OnboardingBriefing:
    """Return a cached title+briefing for one advertising lead row."""
    source = _clean(product)
    if not source:
        return build_heuristic_briefing(
            product=product,
            budget=budget,
            channel=channel,
            region=region,
            client_name=client_name,
        )

    key = _cache_key(source, _clean(budget), _clean(channel), _clean(region))
    cached = _BRIEFING_CACHE.get(key)
    if cached:
        return cached

    mapped = product_title_service.deterministic_short_title(source)
    heuristic = build_heuristic_briefing(
        product=source,
        product_ru=mapped,
        budget=budget,
        channel=channel,
        region=region,
        client_name=client_name,
    )

    payload = {
        "product": source,
        "budget": _clean(budget) or None,
        "contact_channel": _clean(channel) or None,
        "region": _clean(region) or None,
        "client_name": _clean(client_name) or None,
        "marketing_status": _clean(lead_status) or None,
        "hint_short_product_ru": mapped,
    }

    briefing = heuristic
    try:
        briefing = await _ai_briefing(payload)
        # Prefer deterministic short title when we already know a good mapping.
        if mapped:
            briefing = OnboardingBriefing(
                short_product_ru=mapped,
                about_ru=briefing.about_ru,
                talk_points_ru=briefing.talk_points_ru,
                call_goal_ru=briefing.call_goal_ru,
            )
    except Exception as exc:
        logger.warning("Onboarding AI briefing fallback (%s): %s", type(exc).__name__, exc)
        briefing = heuristic

    _BRIEFING_CACHE[key] = briefing
    # Keep the legacy title cache warm so other callers stay fast.
    product_title_service._TITLE_CACHE[product_title_service._cache_key(source)] = (
        briefing.short_product_ru
    )
    return briefing


def clear_briefing_cache() -> None:
    _BRIEFING_CACHE.clear()
