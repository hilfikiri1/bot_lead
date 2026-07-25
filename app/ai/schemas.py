from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CatalogSpecification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    value: str


class CatalogPriceTier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: str
    price: str


class CatalogContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_name_ru: str = Field(min_length=1)
    original_name_zh: str = Field(min_length=1)
    short_description_ru: str = Field(min_length=1)
    supplier_name: str | None = None
    price_display: str = Field(default="Цена уточняется у поставщика.")
    price_note: str | None = None
    moq_display: str | None = None
    price_tiers: list[CatalogPriceTier] = Field(default_factory=list)
    specifications: list[CatalogSpecification] = Field(default_factory=list)
    variants: list[CatalogSpecification] = Field(default_factory=list)
    disclaimer: str = Field(default="Цена и наличие требуют подтверждения перед оформлением заказа.")


def catalog_content_json_schema() -> dict:
    return CatalogContent.model_json_schema()
