"""Catalog rendering tests (HTML always; PDF when Chromium is available)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.schemas import CatalogContent, CatalogPriceTier, CatalogSpecification
from app.catalog.renderer import CatalogRenderer


def _sample_content(price: str = "15.50–22.00 CNY") -> CatalogContent:
    return CatalogContent(
        product_name_ru="Термокружка из нержавеющей стали 500 мл",
        original_name_zh="不锈钢保温杯",
        short_description_ru="Двухслойная вакуумная термокружка объёмом 500 мл.",
        supplier_name="Поставщик из Иу",
        price_display=price,
        price_note=None,
        moq_display="от 2 шт.",
        price_tiers=[
            CatalogPriceTier(quantity="от 2 шт.", price="22.00 CNY"),
            CatalogPriceTier(quantity="от 100 шт.", price="18.50 CNY"),
        ],
        specifications=[
            CatalogSpecification(name="Материал", value="Нержавеющая сталь 304"),
            CatalogSpecification(name="Объём", value="500 мл"),
        ],
        variants=[CatalogSpecification(name="Цвет", value="серебристый/чёрный")],
        disclaimer="Информация носит справочный характер и требует подтверждения.",
    )


def test_render_html_contains_key_fields(settings) -> None:
    renderer = CatalogRenderer(settings)
    context = renderer.build_context(
        _sample_content(),
        source_url="https://detail.1688.com/offer/123.html",
        image_paths=[],
    )
    html = renderer.render_html(context)

    assert "Термокружка из нержавеющей стали 500 мл" in html
    assert "不锈钢保温杯" in html  # Chinese original name present
    assert "PRODUCT CATALOG" in html
    assert "Нержавеющая сталь 304" in html
    assert "22.00 CNY" in html
    assert "detail.1688.com/offer/123.html" in html


def test_render_html_missing_price(settings) -> None:
    renderer = CatalogRenderer(settings)
    context = renderer.build_context(
        _sample_content(price="Цена уточняется у поставщика."),
        source_url="https://detail.1688.com/offer/123.html",
        image_paths=[],
    )
    html = renderer.render_html(context)
    assert "Цена уточняется у поставщика." in html


def test_render_html_uses_text_logo_when_missing(settings) -> None:
    renderer = CatalogRenderer(settings)
    context = renderer.build_context(
        _sample_content(),
        source_url="https://detail.1688.com/offer/123.html",
        image_paths=[],
    )
    html = renderer.render_html(context)
    # No logo file in the test env -> text logo (brand name) should appear.
    assert settings.brand_name in html


@pytest.mark.asyncio
async def test_render_pdf(settings, tmp_path: Path) -> None:
    renderer = CatalogRenderer(settings)
    work_dir = tmp_path / "work"
    output_path = tmp_path / "out" / "catalog.pdf"
    try:
        result = await renderer.render_pdf(
            _sample_content(),
            source_url="https://detail.1688.com/offer/123.html",
            image_paths=[],
            work_dir=work_dir,
            output_path=output_path,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Chromium not available for PDF rendering: {exc}")

    assert result.exists()
    assert result.stat().st_size > 1000
    assert result.read_bytes()[:4] == b"%PDF"
