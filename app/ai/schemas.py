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
    disclaimer: str


CATALOG_CONTENT_SCHEMA = {
    "name": "catalog_content",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "product_name_ru": {"type": "string"},
            "original_name_zh": {"type": "string"},
            "short_description_ru": {"type": "string"},
            "supplier_name": {"type": ["string", "null"]},
            "price_display": {"type": "string"},
            "price_note": {"type": ["string", "null"]},
            "moq_display": {"type": ["string", "null"]},
            "price_tiers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "quantity": {"type": "string"},
                        "price": {"type": "string"},
                    },
                    "required": ["quantity", "price"],
                },
            },
            "specifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["name", "value"],
                },
            },
            "variants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["name", "value"],
                },
            },
            "disclaimer": {"type": "string"},
        },
        "required": [
            "product_name_ru",
            "original_name_zh",
            "short_description_ru",
            "supplier_name",
            "price_display",
            "price_note",
            "moq_display",
            "price_tiers",
            "specifications",
            "variants",
            "disclaimer",
        ],
    },
    "strict": True,
}
