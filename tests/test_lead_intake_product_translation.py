"""Deterministic Russian product translation (mandatory case 24)."""

from __future__ import annotations

import pytest

from app.services import product_title_service


@pytest.mark.asyncio
async def test_narzedzia_translates_to_instrumenty():
    product_title_service.clear_title_cache()
    assert await product_title_service.short_product_title("narzędzia") == "Инструменты"


@pytest.mark.asyncio
async def test_spec_examples_are_deterministic_and_fast():
    product_title_service.clear_title_cache()
    cases = {
        "narzędzia": "Инструменты",
        "fotele autobusowe": "Автобусные сиденья",
        "pokrycia podłogowe": "Напольные покрытия",
        "karmniki i poidła dla drobiu": "Кормушки и поилки для птицы",
    }
    for source, expected in cases.items():
        assert await product_title_service.short_product_title(source) == expected


@pytest.mark.asyncio
async def test_translation_is_concise_with_no_trailing_period():
    product_title_service.clear_title_cache()
    title = await product_title_service.short_product_title("narzędzia")
    assert not title.endswith(".")
    assert len(title) <= 50
