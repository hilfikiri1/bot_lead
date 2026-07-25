"""Pydantic models describing the structured catalog content returned by OpenAI.

These models double as the JSON Schema used with OpenAI Structured Outputs, so
they must stay strict: no extra fields, explicit optionals.
"""

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
    """Russian-language, translated & structured content for the PDF catalog."""

    model_config = ConfigDict(extra="forbid")

    product_name_ru: str = Field(description="Коммерческое название на русском языке")
    original_name_zh: str = Field(description="Оригинальное китайское название")
    short_description_ru: str = Field(description="Нейтральное описание 2-4 предложения")
    supplier_name: str | None = None
    price_display: str = Field(description="Цена или диапазон цен в CNY")
    price_note: str | None = None
    moq_display: str | None = None
    price_tiers: list[CatalogPriceTier] = Field(default_factory=list)
    specifications: list[CatalogSpecification] = Field(default_factory=list)
    variants: list[CatalogSpecification] = Field(default_factory=list)
    disclaimer: str


def catalog_json_schema() -> dict:
    """Return a strict JSON Schema for the CatalogContent model.

    Pydantic's generated schema is post-processed so every object has
    ``additionalProperties: false`` and lists all properties as required, which
    is what OpenAI Structured Outputs (``strict: true``) expects.
    """
    schema = CatalogContent.model_json_schema()
    _strictify(schema, schema.get("$defs", {}))
    return schema


def _strictify(node: dict, defs: dict) -> None:
    node_type = node.get("type")
    if node_type == "object" or "properties" in node:
        node["additionalProperties"] = False
        properties = node.get("properties", {})
        node["required"] = list(properties.keys())
        for prop in properties.values():
            _resolve_and_strictify(prop, defs)
    if node_type == "array" and "items" in node:
        _resolve_and_strictify(node["items"], defs)


def _resolve_and_strictify(node: dict, defs: dict) -> None:
    # Normalize "anyOf" nullable unions (e.g. str | None) — Structured Outputs
    # supports nullable via the "type" array, so collapse simple unions.
    if "anyOf" in node:
        for sub in node["anyOf"]:
            _resolve_and_strictify(sub, defs)
        return
    ref = node.get("$ref")
    if ref:
        name = ref.split("/")[-1]
        target = defs.get(name)
        if target is not None:
            _strictify(target, defs)
        return
    _strictify(node, defs)
