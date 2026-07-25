from __future__ import annotations

from datetime import date
from pathlib import Path

from app.ai.openai_client import OpenAIContentClient
from app.catalog.formatting import format_price_display
from app.catalog.models import CatalogRenderInput
from app.catalog.renderer import CatalogRenderer
from app.config import get_settings
from app.parser.models import ParsedProduct
from app.utils.filenames import build_pdf_filename


class CatalogService:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._ai = OpenAIContentClient()
        self._renderer = CatalogRenderer()

    async def build_catalog(self, product: ParsedProduct, job_dir: Path) -> Path:
        content = await self._ai.generate_catalog_content(product)
        if not content.price_display.strip():
            content.price_display = format_price_display(product)
        if product.price_min_cny is None and product.price_max_cny is None and not content.price_note:
            content.price_note = "Цена уточняется у поставщика."
        output_name = build_pdf_filename(content.product_name_ru or product.title_zh)
        output_path = Path(self._settings.storage_output_dir) / output_name

        payload = CatalogRenderInput(
            content=content,
            source_url=str(product.source_url),
            image_paths=product.local_image_paths,
            generated_date=date.today(),
            brand_name=self._settings.brand_name,
            brand_primary_color=self._settings.brand_primary_color,
            brand_accent_color=self._settings.brand_accent_color,
            brand_text_color=self._settings.brand_text_color,
            logo_path=self._settings.brand_logo_path,
        )
        return await self._renderer.render(payload, output_path, job_dir)
