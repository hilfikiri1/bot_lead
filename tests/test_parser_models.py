"""Tests for parser Pydantic models."""
from __future__ import annotations

from decimal import Decimal
import pytest

from app.parser.models import ParsedProduct, PriceTier, ProductSpecification, ProductVariant


class TestPriceTier:
    def test_all_fields(self):
        tier = PriceTier(
            min_quantity=1,
            max_quantity=99,
            price_cny=Decimal("12.50"),
            raw_text="1-99 | ¥12.50",
        )
        assert tier.price_cny == Decimal("12.50")
        assert tier.min_quantity == 1

    def test_optional_fields_none(self):
        tier = PriceTier()
        assert tier.price_cny is None
        assert tier.min_quantity is None


class TestParsedProduct:
    def test_minimum_valid_product(self):
        p = ParsedProduct(
            source_url="https://detail.1688.com/offer/123.html",
            title_zh="工业风扇",
            gallery_image_urls=["https://img.1688.com/img/1.jpg"],
        )
        assert p.has_minimum_data() is True

    def test_no_images_fails_minimum(self):
        p = ParsedProduct(
            source_url="https://detail.1688.com/offer/123.html",
            title_zh="工业风扇",
        )
        assert p.has_minimum_data() is False

    def test_empty_title_raises(self):
        with pytest.raises(ValueError):
            ParsedProduct(
                source_url="https://detail.1688.com/offer/123.html",
                title_zh="   ",
                gallery_image_urls=["https://img.1688.com/img/1.jpg"],
            )

    def test_decimal_price(self):
        p = ParsedProduct(
            source_url="https://detail.1688.com/offer/123.html",
            title_zh="商品",
            price_min_cny=Decimal("10.50"),
            price_max_cny=Decimal("25.00"),
            gallery_image_urls=["https://img.1688.com/img/1.jpg"],
        )
        assert p.price_min_cny == Decimal("10.50")
        assert p.price_max_cny == Decimal("25.00")

    def test_full_product(self):
        p = ParsedProduct(
            source_url="https://detail.1688.com/offer/123.html",
            title_zh="工业风扇",
            supplier_name_zh="深圳某公司",
            price_min_cny=Decimal("50"),
            price_max_cny=Decimal("120"),
            price_raw_text="¥50~¥120",
            price_tiers=[
                PriceTier(min_quantity=1, max_quantity=99, price_cny=Decimal("120"))
            ],
            moq=100,
            moq_raw_text="100件",
            variants=[ProductVariant(name="颜色", values=["红色", "蓝色"])],
            specifications=[ProductSpecification(name_zh="功率", value_zh="50W")],
            gallery_image_urls=["https://img.1688.com/img/1.jpg"],
            detail_image_urls=["https://img.1688.com/desc/2.jpg"],
        )
        assert p.has_minimum_data()
        assert p.moq == 100
        assert len(p.price_tiers) == 1
        assert len(p.variants) == 1
        assert len(p.specifications) == 1


class TestPriceDecimalNotFloat:
    """Ensure prices use Decimal, not float, to avoid rounding errors."""

    def test_no_float_in_prices(self):
        p = ParsedProduct(
            source_url="https://detail.1688.com/offer/1.html",
            title_zh="商品",
            price_min_cny=Decimal("12.33"),
            gallery_image_urls=["https://img.1688.com/1.jpg"],
        )
        assert isinstance(p.price_min_cny, Decimal)
        assert not isinstance(p.price_min_cny, float)
