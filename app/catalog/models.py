from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic import BaseModel

from app.ai.schemas import CatalogContent
from app.parser.models import ParsedProduct


class CatalogRenderData(BaseModel):
    brand_name: str
    brand_primary_color: str
    brand_accent_color: str
    brand_text_color: str
    brand_logo_path: str | None
    brand_website: str | None = None
    brand_email: str | None = None
    brand_phone: str | None = None
    content: CatalogContent
    product: ParsedProduct
    image_paths: list[str]
    created_date: date
    source_url: str
    output_html: Path
    output_pdf: Path
