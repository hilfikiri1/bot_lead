"""Tests for catalog PDF renderer."""

from decimal import Decimal

from app.ai.schemas import CatalogContent
from app.catalog.renderer import CatalogRenderer
from app.parser.models import ParsedProduct


class TestCatalogRenderer:
    def test_render_html_without_logo(self):
        renderer = CatalogRenderer()
        content = CatalogContent(
            product_name_ru="Промышленный клапан",
            original_name_zh="工业阀门",
            short_description_ru="Описание товара для тестового каталога.",
            price_display="¥ 45.00 – ¥ 52.00",
            moq_display="100 шт.",
            supplier_name="Тестовый поставщик",
        )
        product = ParsedProduct(
            source_url="https://detail.1688.com/offer/123.html",
            title_zh="工业阀门",
            price_min_cny=Decimal("45.00"),
            price_max_cny=Decimal("52.00"),
        )
        html = renderer.render_html_string(content, product)
        assert "Промышленный клапан" in html
        assert "工业阀门" in html
        assert "¥ 45.00" in html
        assert "Babrik Solutions" in html

    def test_render_html_without_price(self):
        renderer = CatalogRenderer()
        content = CatalogContent(
            product_name_ru="Товар без цены",
            original_name_zh="无价格商品",
            short_description_ru="Описание.",
            price_display="Цена уточняется у поставщика.",
        )
        product = ParsedProduct(
            source_url="https://detail.1688.com/offer/456.html",
            title_zh="无价格商品",
        )
        html = renderer.render_html_string(content, product)
        assert "уточняется у поставщика" in html

    def test_no_specs_section_when_empty(self):
        renderer = CatalogRenderer()
        content = CatalogContent(
            product_name_ru="Товар",
            original_name_zh="商品",
            short_description_ru="Описание.",
            price_display="¥ 10.00",
            specifications=[],
        )
        product = ParsedProduct(
            source_url="https://detail.1688.com/offer/789.html",
            title_zh="商品",
        )
        html = renderer.render_html_string(content, product)
        assert "Характеристики" not in html
