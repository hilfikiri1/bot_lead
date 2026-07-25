"""OpenAI structured output schemas."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


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


class BatchCatalogSection(BaseModel):
    content: CatalogContent
    source_url: str
    main_image: str | None = None


class BatchCatalogRenderContext(BaseModel):
    brand_name: str
    brand_primary_color: str
    brand_accent_color: str
    brand_text_color: str
    brand_website: str = ""
    brand_email: str = ""
    brand_phone: str = ""
    logo_path: str | None = None
    collection_title: str
    source_page_url: str | None = None
    created_date: date = Field(default_factory=date.today)
    sections: list[BatchCatalogSection] = Field(default_factory=list)
    css_path: str = ""
    disclaimer: str = (
        "Информация в каталоге сформирована автоматически на основании данных, "
        "размещённых поставщиком на платформе 1688.com."
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)


def catalog_content_json_schema() -> dict:
    """Return JSON Schema for OpenAI Structured Outputs."""
    return CatalogContent.model_json_schema()
