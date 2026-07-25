"""
Tests for the catalog PDF renderer.
Uses fixture data — no real 1688 or OpenAI calls.
PDF rendering uses Playwright (headless Chromium must be installed).
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.catalog.models import RenderContext
from app.catalog.renderer import PDFRenderer

# ── Fixtures ──────────────────────────────────────────────────────────────────

def _create_test_image(path: Path, width: int = 600, height: int = 600) -> None:
    img = Image.new("RGB", (width, height), color=(70, 130, 180))
    img.save(str(path), format="JPEG")


@pytest.fixture
def sample_render_context(tmp_path: Path) -> RenderContext:
    img_path = tmp_path / "test_product.jpg"
    _create_test_image(img_path)

    return RenderContext(
        product_name_ru="Промышленный осевой вентилятор",
        original_name_zh="工业轴流风机",
        short_description_ru=(
            "Промышленный осевой вентилятор для систем вентиляции и охлаждения. "
            "Подходит для использования в производственных помещениях."
        ),
        supplier_name="Shenzhen Industry Co.",
        price_display="¥85,00 – ¥150,00",
        price_note=None,
        moq_display="50 шт.",
        price_tiers=[
            {"quantity": "1–49", "price": "¥150,00"},
            {"quantity": "50–199", "price": "¥110,00"},
            {"quantity": "200+", "price": "¥85,00"},
        ],
        specifications=[
            {"name": "Мощность", "value": "250 Вт"},
            {"name": "Напряжение", "value": "220 В"},
            {"name": "Диаметр", "value": "400 мм"},
        ],
        variants=[
            {"name": "Цвет", "value": "Серый, Белый"},
        ],
        disclaimer=(
            "Информация сформирована автоматически. "
            "Уточняйте данные у поставщика."
        ),
        source_url="https://detail.1688.com/offer/123456789.html",
        local_image_paths=[str(img_path)],
        created_date="2025-01-15",
        brand_name="Babrik Solutions",
        brand_primary_color="#0B1F3A",
        brand_accent_color="#D8A34A",
        brand_text_color="#20242A",
        brand_website="www.babrik.com",
        brand_email="info@babrik.com",
        brand_phone="+7 (999) 123-45-67",
        logo_path=None,  # No logo in tests; should not crash
    )


class TestPDFRendererHTML:
    """Test HTML rendering without PDF export."""

    def test_html_contains_product_name(self, sample_render_context: RenderContext):
        renderer = PDFRenderer()
        html = renderer._render_html(sample_render_context)
        assert "Промышленный осевой вентилятор" in html

    def test_html_contains_chinese_name(self, sample_render_context: RenderContext):
        renderer = PDFRenderer()
        html = renderer._render_html(sample_render_context)
        assert "工业轴流风机" in html

    def test_html_contains_price(self, sample_render_context: RenderContext):
        renderer = PDFRenderer()
        html = renderer._render_html(sample_render_context)
        assert "¥85,00" in html

    def test_html_contains_brand(self, sample_render_context: RenderContext):
        renderer = PDFRenderer()
        html = renderer._render_html(sample_render_context)
        assert "Babrik Solutions" in html

    def test_html_contains_specs(self, sample_render_context: RenderContext):
        renderer = PDFRenderer()
        html = renderer._render_html(sample_render_context)
        assert "Мощность" in html
        assert "250 Вт" in html

    def test_html_no_logo_fallback(self, sample_render_context: RenderContext):
        """When logo_path is None, brand text should appear instead of img tag."""
        renderer = PDFRenderer()
        html = renderer._render_html(sample_render_context)
        # Logo path is None — should use text logo
        assert "brand-text-logo" in html

    def test_html_source_url_present(self, sample_render_context: RenderContext):
        renderer = PDFRenderer()
        html = renderer._render_html(sample_render_context)
        assert "detail.1688.com" in html

    def test_html_disclaimer_present(self, sample_render_context: RenderContext):
        renderer = PDFRenderer()
        html = renderer._render_html(sample_render_context)
        assert "уточняйте данные у поставщика" in html.lower()


class TestRenderContextWithNoPrice:
    def test_no_price_renders_without_crash(self, tmp_path: Path):
        img_path = tmp_path / "img.jpg"
        _create_test_image(img_path)

        ctx = RenderContext(
            product_name_ru="Товар без цены",
            original_name_zh="无价格商品",
            short_description_ru="Описание.",
            supplier_name=None,
            price_display="Цена уточняется у поставщика.",
            price_note=None,
            moq_display=None,
            price_tiers=[],
            specifications=[],
            variants=[],
            disclaimer="Данные требуют подтверждения.",
            source_url="https://detail.1688.com/offer/999.html",
            local_image_paths=[str(img_path)],
            created_date="2025-01-15",
            brand_name="Babrik Solutions",
            brand_primary_color="#0B1F3A",
            brand_accent_color="#D8A34A",
            brand_text_color="#20242A",
            brand_website="",
            brand_email="",
            brand_phone="",
            logo_path=None,
        )
        renderer = PDFRenderer()
        html = renderer._render_html(ctx)
        assert "Цена уточняется у поставщика." in html
        assert "Товар без цены" in html


@pytest.mark.asyncio
async def test_pdf_render_produces_file(
    tmp_path: Path, sample_render_context: RenderContext
):
    """
    End-to-end PDF render test. Requires Playwright Chromium to be installed.
    Skipped automatically if Playwright is not available.
    """
    pytest.importorskip("playwright")

    output_pdf = tmp_path / "test_catalog.pdf"
    renderer = PDFRenderer()

    result_path = await renderer.render(sample_render_context, output_pdf)
    assert result_path.exists()
    assert result_path.stat().st_size > 10_000  # Expect at least 10 KB
