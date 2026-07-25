from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.ai.openai_client import OpenAIClient
from app.ai.schemas import CatalogContent
from app.catalog.models import RenderContext
from app.catalog.renderer import PDFRenderer
from app.config import settings
from app.logging_config import get_logger
from app.parser.browser import open_product_page
from app.parser.image_downloader import download_images
from app.parser.models import ParsedProduct
from app.parser.parser_1688 import parse_product_page
from app.parser.session_manager import browser_manager
from app.services.task_service import TaskService

logger = get_logger(__name__)


class CatalogService:
    """
    Orchestrates the full pipeline for a single catalog generation job.
    """

    def __init__(self, job_id: uuid.UUID, task_service: TaskService) -> None:
        self._job_id = job_id
        self._task_service = task_service
        self._job_dir = Path(settings.temp_storage_dir) / str(job_id)
        self._job_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_product(self, url: str) -> ParsedProduct:
        async with browser_manager.new_context() as context:
            page = await open_product_page(context, url, job_dir=self._job_dir)
            try:
                product = await parse_product_page(page, url, job_dir=self._job_dir)
            finally:
                await page.close()
        await self._task_service.update_status(
            self._job_id,
            "parsing",
            product_title=product.title_zh[:200],
        )
        return product

    async def download_images(self, product: ParsedProduct) -> None:
        images_dir = self._job_dir / "images"
        local_paths = await download_images(
            gallery_urls=product.gallery_image_urls,
            detail_urls=product.detail_image_urls,
            save_dir=images_dir,
            referer=product.source_url,
        )
        product.local_image_paths = local_paths

    async def generate_content(self, product: ParsedProduct) -> CatalogContent:
        client = OpenAIClient()
        main_image: Optional[Path] = None
        if product.local_image_paths:
            main_image = Path(product.local_image_paths[0])
        return await client.generate_catalog_content(product, main_image_path=main_image)

    async def render_pdf(
        self, product: ParsedProduct, content: CatalogContent
    ) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        output_dir = Path(settings.output_storage_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / f"{self._job_id}.pdf"

        ctx = RenderContext(
            product_name_ru=content.product_name_ru,
            original_name_zh=content.original_name_zh,
            short_description_ru=content.short_description_ru,
            supplier_name=content.supplier_name,
            price_display=content.price_display,
            price_note=content.price_note,
            moq_display=content.moq_display,
            price_tiers=[{"quantity": t.quantity, "price": t.price} for t in content.price_tiers],
            specifications=[{"name": s.name, "value": s.value} for s in content.specifications],
            variants=[{"name": v.name, "value": v.value} for v in content.variants],
            disclaimer=content.disclaimer,
            source_url=product.source_url,
            local_image_paths=product.local_image_paths,
            created_date=today,
            brand_name=settings.brand_name,
            brand_primary_color=settings.brand_primary_color,
            brand_accent_color=settings.brand_accent_color,
            brand_text_color=settings.brand_text_color,
            brand_website=settings.brand_website,
            brand_email=settings.brand_email,
            brand_phone=settings.brand_phone,
            logo_path=settings.brand_logo_path or None,
        )

        renderer = PDFRenderer()
        return await renderer.render(ctx, pdf_path)

    async def cleanup_temp_files(self) -> None:
        """Delete temporary images and HTML from the job directory."""
        import shutil
        try:
            if self._job_dir.exists():
                shutil.rmtree(self._job_dir)
                logger.debug("temp_dir_removed", path=str(self._job_dir))
        except Exception as exc:
            logger.warning("temp_cleanup_failed", error=str(exc))
