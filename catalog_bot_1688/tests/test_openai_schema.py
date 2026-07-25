"""Structured Output schema and CatalogContent model tests."""

from __future__ import annotations

from app.ai.schemas import (
    CatalogContent,
    CatalogPriceTier,
    CatalogSpecification,
    catalog_json_schema,
)


def _walk(node: dict):
    yield node
    for value in node.values():
        if isinstance(value, dict):
            yield from _walk(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from _walk(item)


def test_schema_is_strict_top_level() -> None:
    schema = catalog_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    # Every declared property must be required for strict Structured Outputs.
    assert set(schema["required"]) == set(schema["properties"].keys())


def test_schema_objects_forbid_additional_properties() -> None:
    schema = catalog_json_schema()
    defs = schema.get("$defs", {})
    for definition in defs.values():
        if definition.get("type") == "object" or "properties" in definition:
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"].keys())


def test_catalog_content_validates() -> None:
    content = CatalogContent(
        product_name_ru="Термокружка из нержавеющей стали",
        original_name_zh="不锈钢保温杯",
        short_description_ru="Двухслойная вакуумная кружка. Объём 500 мл.",
        supplier_name="Поставщик из Иу",
        price_display="15.50–22.00 CNY",
        price_note=None,
        moq_display="от 2 шт.",
        price_tiers=[CatalogPriceTier(quantity="от 2 шт.", price="22.00 CNY")],
        specifications=[CatalogSpecification(name="Материал", value="Нержавеющая сталь 304")],
        variants=[CatalogSpecification(name="Цвет", value="серебристый/чёрный/синий")],
        disclaimer="Информация носит справочный характер.",
    )
    assert content.product_name_ru
    assert content.price_tiers[0].price == "22.00 CNY"


def test_catalog_content_defaults_optional_lists() -> None:
    content = CatalogContent(
        product_name_ru="Товар",
        original_name_zh="商品",
        short_description_ru="Описание.",
        price_display="Цена уточняется у поставщика.",
        disclaimer="Дисклеймер.",
    )
    assert content.price_tiers == []
    assert content.specifications == []
    assert content.supplier_name is None
