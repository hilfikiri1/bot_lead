from decimal import Decimal

from app.ai.schemas import CATALOG_CONTENT_SCHEMA, CatalogContent
from app.catalog.formatting import format_price_display
from app.parser.models import ParsedProduct
from app.utils.filenames import sanitize_filename_part


def test_catalog_schema_has_required_keys() -> None:
    required = CATALOG_CONTENT_SCHEMA["schema"]["required"]
    assert "product_name_ru" in required
    assert "price_display" in required
    assert CATALOG_CONTENT_SCHEMA["strict"] is True


def test_catalog_content_model_validation() -> None:
    model = CatalogContent.model_validate(
        {
            "product_name_ru": "Тестовый товар",
            "original_name_zh": "测试商品",
            "short_description_ru": "Нейтральное описание товара в 2-4 предложениях.",
            "supplier_name": None,
            "price_display": "10-12 CNY",
            "price_note": None,
            "moq_display": None,
            "price_tiers": [],
            "specifications": [],
            "variants": [],
            "disclaimer": "Информация уточняется у поставщика.",
        }
    )
    assert model.product_name_ru == "Тестовый товар"


def test_filename_normalization() -> None:
    assert sanitize_filename_part(" Тест / product ### ") == "Тест_product"


def test_price_formatting_and_missing_price() -> None:
    parsed = ParsedProduct(
        source_url="https://detail.1688.com/offer/1.html",
        title_zh="测试",
        price_min_cny=Decimal("10.00"),
        price_max_cny=Decimal("12.50"),
        gallery_image_urls=["https://img.example.com/1.jpg"],
        detail_image_urls=[],
    )
    assert format_price_display(parsed) == "10–12.5 CNY"

    parsed_no_price = ParsedProduct(
        source_url="https://detail.1688.com/offer/1.html",
        title_zh="测试",
        gallery_image_urls=["https://img.example.com/1.jpg"],
        detail_image_urls=[],
    )
    assert format_price_display(parsed_no_price) == "Цена уточняется у поставщика."
