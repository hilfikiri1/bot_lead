from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


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
    supplier_name: Optional[str] = None
    price_display: str
    price_note: Optional[str] = None
    moq_display: Optional[str] = None
    price_tiers: list[CatalogPriceTier] = []
    specifications: list[CatalogSpecification] = []
    variants: list[CatalogSpecification] = []
    disclaimer: str


# JSON Schema for OpenAI Structured Outputs
CATALOG_CONTENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "product_name_ru": {
            "type": "string",
            "description": "Коммерческое название товара на русском языке",
        },
        "original_name_zh": {
            "type": "string",
            "description": "Оригинальное название товара на китайском языке",
        },
        "short_description_ru": {
            "type": "string",
            "description": "Краткое нейтральное описание товара на русском, 2-4 предложения",
        },
        "supplier_name": {
            "type": ["string", "null"],
            "description": "Название поставщика",
        },
        "price_display": {
            "type": "string",
            "description": "Отображаемая цена в юанях, например: '¥12,50 – ¥30,00'",
        },
        "price_note": {
            "type": ["string", "null"],
            "description": "Пояснение к цене, если необходимо",
        },
        "moq_display": {
            "type": ["string", "null"],
            "description": "Минимальный заказ в читаемом виде",
        },
        "price_tiers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quantity": {"type": "string"},
                    "price": {"type": "string"},
                },
                "required": ["quantity", "price"],
                "additionalProperties": False,
            },
            "description": "Ступенчатые цены",
        },
        "specifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["name", "value"],
                "additionalProperties": False,
            },
            "description": "Технические характеристики",
        },
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["name", "value"],
                "additionalProperties": False,
            },
            "description": "Варианты товара (цвет, размер и т.д.)",
        },
        "disclaimer": {
            "type": "string",
            "description": "Стандартный дисклеймер о необходимости уточнения данных у поставщика",
        },
    },
    "required": [
        "product_name_ru",
        "original_name_zh",
        "short_description_ru",
        "price_display",
        "disclaimer",
    ],
    "additionalProperties": False,
}
