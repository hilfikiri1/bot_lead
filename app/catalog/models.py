from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RenderContext:
    """All data needed to render a PDF catalog page."""
    product_name_ru: str
    original_name_zh: str
    short_description_ru: str
    supplier_name: str | None
    price_display: str
    price_note: str | None
    moq_display: str | None
    price_tiers: list[dict]       # [{"quantity": str, "price": str}]
    specifications: list[dict]    # [{"name": str, "value": str}]
    variants: list[dict]          # [{"name": str, "value": str}]
    disclaimer: str
    source_url: str
    local_image_paths: list[str]
    created_date: str             # YYYY-MM-DD
    brand_name: str
    brand_primary_color: str
    brand_accent_color: str
    brand_text_color: str
    brand_website: str
    brand_email: str
    brand_phone: str
    logo_path: str | None = None  # Absolute path to logo file or None
