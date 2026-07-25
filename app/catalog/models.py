"""Catalog rendering models."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.ai.schemas import CatalogContent


class CatalogRenderContext(BaseModel):
    brand_name: str
    brand_primary_color: str
    brand_accent_color: str
    brand_text_color: str
    brand_website: str = ""
    brand_email: str = ""
    brand_phone: str = ""
    logo_path: str | None = None
    content: CatalogContent
    source_url: str
    created_date: date = Field(default_factory=date.today)
    main_image: str | None = None
    gallery_images: list[str] = Field(default_factory=list)
    css_path: str = ""

    model_config = ConfigDict(arbitrary_types_allowed=True)
