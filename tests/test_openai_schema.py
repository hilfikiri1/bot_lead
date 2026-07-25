import pytest
from pydantic import ValidationError

from app.ai.schemas import CatalogContent, catalog_content_json_schema
from app.parser.models import ParsedProduct, PriceTier


def test_catalog_content_schema_has_required_fields():
    schema = catalog_content_json_schema()
    assert "product_name_ru" in schema["properties"]
    assert schema["additionalProperties"] is False


def test_catalog_content_validates_required_values():
    content = CatalogContent(product_name_ru="Термокружка", original_name_zh="保温杯", short_description_ru="Краткое описание товара.")
    assert content.price_display == "Цена уточняется у поставщика."


def test_catalog_content_rejects_extra_fields():
    with pytest.raises(ValidationError):
        CatalogContent.model_validate({"product_name_ru":"A","original_name_zh":"B","short_description_ru":"C","fake":"D"})


def test_parsed_product_uses_decimal_prices():
    product = ParsedProduct(source_url="https://detail.1688.com/offer/1.html", title_zh="杯子", price_tiers=[PriceTier(price_cny="12.50")], gallery_image_urls=["https://cbu01.alicdn.com/a.jpg"])
    assert str(product.price_tiers[0].price_cny) == "12.50"
