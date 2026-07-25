"""OpenAI structured output schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CatalogSpecification(BaseModel):
    name: str
    value: str


class CatalogPriceTier(BaseModel):
    quantity: str
    price: str


class CatalogContent(BaseModel):
    product_name_ru: str
    original_name_zh: str
    short_description_ru: str
    supplier_name: str | None = None
    price_display: str
    price_note: str | None = None
    moq_display: str | None = None
    price_tiers: list[CatalogPriceTier] = Field(default_factory=list)
    specifications: list[CatalogSpecification] = Field(default_factory=list)
    variants: list[CatalogSpecification] = Field(default_factory=list)
    disclaimer: str = (
        "Информация в каталоге сформирована автоматически на основании данных, "
        "размещённых поставщиком на платформе 1688.com."
    )


def catalog_content_json_schema() -> dict:
    """Return JSON Schema for OpenAI Structured Outputs."""
    return CatalogContent.model_json_schema()
