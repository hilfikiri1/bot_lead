"""Tests for OpenAI schema and CatalogContent model."""
from __future__ import annotations

import json

import pytest

from app.ai.schemas import (
    CATALOG_CONTENT_SCHEMA,
    CatalogContent,
    CatalogPriceTier,
    CatalogSpecification,
)
from app.ai.openai_client import _build_catalog_content
from app.ai.prompts import CATALOG_DISCLAIMER, build_user_message


class TestCatalogContentModel:
    def test_full_content(self):
        content = CatalogContent(
            product_name_ru="Промышленный вентилятор",
            original_name_zh="工业风扇",
            short_description_ru="Промышленный осевой вентилятор для вентиляции помещений.",
            supplier_name="Шэньчжэньская компания",
            price_display="¥50,00 – ¥120,00",
            price_note=None,
            moq_display="100 шт.",
            price_tiers=[CatalogPriceTier(quantity="1–99", price="¥120,00")],
            specifications=[CatalogSpecification(name="Мощность", value="50 Вт")],
            variants=[CatalogSpecification(name="Цвет", value="Белый, Серый")],
            disclaimer=CATALOG_DISCLAIMER,
        )
        assert content.product_name_ru == "Промышленный вентилятор"
        assert len(content.price_tiers) == 1
        assert content.price_tiers[0].quantity == "1–99"

    def test_minimal_required_fields(self):
        content = CatalogContent(
            product_name_ru="Товар",
            original_name_zh="商品",
            short_description_ru="Описание.",
            price_display="Цена уточняется у поставщика.",
            disclaimer=CATALOG_DISCLAIMER,
        )
        assert content.supplier_name is None
        assert content.price_tiers == []
        assert content.specifications == []

    def test_missing_required_field_raises(self):
        with pytest.raises(Exception):
            CatalogContent(
                original_name_zh="商品",
                short_description_ru="Описание.",
                price_display="¥10,00",
                disclaimer="...",
                # product_name_ru is missing
            )


class TestCatalogContentSchema:
    def test_schema_is_valid_json(self):
        json_str = json.dumps(CATALOG_CONTENT_SCHEMA)
        loaded = json.loads(json_str)
        assert loaded["type"] == "object"

    def test_required_fields_present(self):
        required = CATALOG_CONTENT_SCHEMA.get("required", [])
        assert "product_name_ru" in required
        assert "original_name_zh" in required
        assert "price_display" in required
        assert "disclaimer" in required

    def test_additional_properties_false(self):
        assert CATALOG_CONTENT_SCHEMA.get("additionalProperties") is False

    def test_price_tiers_array(self):
        tiers_schema = CATALOG_CONTENT_SCHEMA["properties"]["price_tiers"]
        assert tiers_schema["type"] == "array"
        assert "items" in tiers_schema


class TestBuildCatalogContent:
    def test_builds_from_complete_dict(self):
        data = {
            "product_name_ru": "Тестовый товар",
            "original_name_zh": "测试商品",
            "short_description_ru": "Описание товара.",
            "supplier_name": "Поставщик",
            "price_display": "¥25,00",
            "price_note": None,
            "moq_display": "50 шт.",
            "price_tiers": [{"quantity": "1–49", "price": "¥30,00"}],
            "specifications": [{"name": "Вес", "value": "500 г"}],
            "variants": [{"name": "Размер", "value": "M, L"}],
            "disclaimer": CATALOG_DISCLAIMER,
        }
        content = _build_catalog_content(data)
        assert content.product_name_ru == "Тестовый товар"
        assert content.supplier_name == "Поставщик"
        assert len(content.price_tiers) == 1
        assert content.price_tiers[0].price == "¥30,00"

    def test_uses_default_disclaimer_when_missing(self):
        data = {
            "product_name_ru": "Товар",
            "original_name_zh": "商品",
            "short_description_ru": "Описание.",
            "price_display": "Уточнять",
            "price_tiers": [],
            "specifications": [],
            "variants": [],
        }
        content = _build_catalog_content(data)
        assert CATALOG_DISCLAIMER in content.disclaimer


class TestBuildUserMessage:
    def test_includes_title(self):
        msg = build_user_message(
            title_zh="工业风扇",
            price_raw="¥50–120",
            price_tiers_raw=["1–49: ¥120"],
            moq_raw="100 pcs",
            specifications=[("尺寸", "30cm")],
            variants=[("颜色", ["红色", "蓝色"])],
            supplier="深圳公司",
        )
        assert "工业风扇" in msg
        assert "深圳公司" in msg
        assert "¥50–120" in msg
        assert "1–49: ¥120" in msg
        assert "100 pcs" in msg

    def test_no_price_shows_not_specified(self):
        msg = build_user_message(
            title_zh="商品",
            price_raw=None,
            price_tiers_raw=[],
            moq_raw=None,
            specifications=[],
            variants=[],
            supplier=None,
        )
        assert "не указана" in msg
