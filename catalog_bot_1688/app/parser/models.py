"""Pydantic models describing the raw data extracted from a 1688 product page."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class PriceTier(BaseModel):
    """A single quantity-based price step."""

    min_quantity: int | None = None
    max_quantity: int | None = None
    price_cny: Decimal | None = None
    raw_text: str | None = None


class ProductVariant(BaseModel):
    """A product variant/option group (e.g. color, size)."""

    name: str
    values: list[str] = Field(default_factory=list)


class ProductSpecification(BaseModel):
    """A raw Chinese specification key/value pair."""

    name_zh: str
    value_zh: str


class ParsedProduct(BaseModel):
    """Structured, best-effort result of parsing a 1688 product page.

    Missing values are represented as ``None`` / empty lists. The parser never
    invents data — absent fields simply stay empty.
    """

    source_url: str
    title_zh: str
    supplier_name_zh: str | None = None
    price_min_cny: Decimal | None = None
    price_max_cny: Decimal | None = None
    price_raw_text: str | None = None
    price_tiers: list[PriceTier] = Field(default_factory=list)
    moq: int | None = None
    moq_raw_text: str | None = None
    variants: list[ProductVariant] = Field(default_factory=list)
    specifications: list[ProductSpecification] = Field(default_factory=list)
    gallery_image_urls: list[str] = Field(default_factory=list)
    detail_image_urls: list[str] = Field(default_factory=list)
    local_image_paths: list[str] = Field(default_factory=list)

    def has_minimum_data(self) -> bool:
        """Minimum viable result: a title plus at least one candidate image."""
        has_image = bool(
            self.gallery_image_urls
            or self.detail_image_urls
            or self.local_image_paths
        )
        return bool(self.title_zh.strip()) and has_image
