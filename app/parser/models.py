from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, field_validator


class PriceTier(BaseModel):
    min_quantity: Optional[int] = None
    max_quantity: Optional[int] = None
    price_cny: Optional[Decimal] = None
    raw_text: Optional[str] = None


class ProductVariant(BaseModel):
    name: str
    values: list[str]


class ProductSpecification(BaseModel):
    name_zh: str
    value_zh: str


class ParsedProduct(BaseModel):
    source_url: str
    title_zh: str
    supplier_name_zh: Optional[str] = None
    price_min_cny: Optional[Decimal] = None
    price_max_cny: Optional[Decimal] = None
    price_raw_text: Optional[str] = None
    price_tiers: list[PriceTier] = []
    moq: Optional[int] = None
    moq_raw_text: Optional[str] = None
    variants: list[ProductVariant] = []
    specifications: list[ProductSpecification] = []
    gallery_image_urls: list[str] = []
    detail_image_urls: list[str] = []
    # Populated after download
    local_image_paths: list[str] = []

    @field_validator("title_zh", mode="before")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        v = str(v).strip()
        if not v:
            raise ValueError("title_zh cannot be empty")
        return v

    def has_minimum_data(self) -> bool:
        """Check that we have at least a title and one image URL."""
        return bool(self.title_zh) and bool(
            self.gallery_image_urls or self.detail_image_urls
        )
