"""Parsed product model + price handling tests."""

from __future__ import annotations

from decimal import Decimal

from app.parser.models import (
    ParsedProduct,
    PriceTier,
    ProductSpecification,
    ProductVariant,
)


def test_price_tier_uses_decimal() -> None:
    tier = PriceTier(min_quantity=100, price_cny=Decimal("18.50"), raw_text="100+: 18.50")
    assert isinstance(tier.price_cny, Decimal)
    assert tier.price_cny == Decimal("18.50")


def test_parsed_product_minimum_data() -> None:
    product = ParsedProduct(
        source_url="https://detail.1688.com/offer/1.html",
        title_zh="保温杯",
        gallery_image_urls=["https://cbu01.alicdn.com/img/a.jpg"],
    )
    assert product.has_minimum_data() is True


def test_parsed_product_missing_image_is_not_minimum() -> None:
    product = ParsedProduct(
        source_url="https://detail.1688.com/offer/1.html",
        title_zh="保温杯",
    )
    assert product.has_minimum_data() is False


def test_parsed_product_missing_title_is_not_minimum() -> None:
    product = ParsedProduct(
        source_url="https://detail.1688.com/offer/1.html",
        title_zh="   ",
        gallery_image_urls=["https://cbu01.alicdn.com/img/a.jpg"],
    )
    assert product.has_minimum_data() is False


def test_full_product_model() -> None:
    product = ParsedProduct(
        source_url="https://detail.1688.com/offer/1.html",
        title_zh="不锈钢保温杯",
        supplier_name_zh="义乌公司",
        price_min_cny=Decimal("15.50"),
        price_max_cny=Decimal("22.00"),
        price_raw_text="15.50-22.00",
        price_tiers=[PriceTier(min_quantity=2, price_cny=Decimal("22.00"))],
        moq=2,
        variants=[ProductVariant(name="颜色", values=["银色", "黑色"])],
        specifications=[ProductSpecification(name_zh="材质", value_zh="304不锈钢")],
        gallery_image_urls=["https://cbu01.alicdn.com/img/a.jpg"],
    )
    assert product.price_max_cny == Decimal("22.00")
    assert product.variants[0].values == ["银色", "黑色"]
    assert product.specifications[0].name_zh == "材质"
