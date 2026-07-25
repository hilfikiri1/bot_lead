"""Tests for OpenAI structured output schema."""

from app.ai.schemas import CatalogContent, CatalogPriceTier, CatalogSpecification, catalog_content_json_schema


class TestCatalogContentSchema:
    def test_valid_model(self):
        content = CatalogContent(
            product_name_ru="Тестовый товар",
            original_name_zh="测试商品",
            short_description_ru="Краткое описание товара для каталога.",
            price_display="¥ 12.50",
            price_tiers=[
                CatalogPriceTier(quantity="100–499 шт.", price="¥ 12.50"),
            ],
            specifications=[
                CatalogSpecification(name="Материал", value="уточняется у поставщика"),
            ],
        )
        assert content.product_name_ru == "Тестовый товар"
        assert len(content.price_tiers) == 1

    def test_json_schema_has_required_fields(self):
        schema = catalog_content_json_schema()
        assert "properties" in schema
        assert "product_name_ru" in schema["properties"]
        assert "price_display" in schema["properties"]
        assert "short_description_ru" in schema["properties"]

    def test_missing_price_defaults_in_client(self):
        content = CatalogContent(
            product_name_ru="Товар",
            original_name_zh="商品",
            short_description_ru="Описание.",
            price_display="Цена уточняется у поставщика.",
        )
        assert "уточняется" in content.price_display
