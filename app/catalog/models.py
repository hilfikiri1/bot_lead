from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.ai.schemas import CatalogContent


class CatalogRenderInput(BaseModel):
    content: CatalogContent
    source_url: str
    image_paths: list[str]
    generated_date: date
    brand_name: str
    brand_primary_color: str
    brand_accent_color: str
    brand_text_color: str
    logo_path: str | None = None
