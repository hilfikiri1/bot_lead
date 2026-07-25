"""Pydantic models for parsed 1688 product data."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class PriceTier(BaseModel):
    min_quantity: int | None = None
    max_quantity: int | None = None
    price_cny: Decimal | None = None
    raw_text: str | None = None


class ProductVariant(BaseModel):
    name: str
    values: list[str]


class ProductSpecification(BaseModel):
    name_zh: str
    value_zh: str


class ParsedProduct(BaseModel):
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
        return bool(self.title_zh and (self.gallery_image_urls or self.local_image_paths))
