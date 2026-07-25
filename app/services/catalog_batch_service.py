"""Batch catalog generation from Chrome extension product lists."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update

from app.ai.openai_client import OpenAICatalogClient
from app.ai.schemas import BatchCatalogSection, CatalogContent
from app.api.catalog_schemas import BatchProductInput
from app.catalog.renderer import CatalogRenderer
from app.catalog_exceptions import CatalogBotError
from app.config import get_settings
from app.models.catalog_job import CatalogJob
from app.parser.image_downloader import ImageDownloader
from app.parser.models import ParsedProduct
from app.utils.filenames import build_batch_pdf_filename

logger = logging.getLogger(__name__)
settings = get_settings()


def batch_input_to_parsed_product(item: BatchProductInput) -> ParsedProduct:
    gallery_urls: list[str] = []
    if item.thumbnail_url:
        gallery_urls.append(item.thumbnail_url)
    return ParsedProduct(
        source_url=item.source_url,
        title_zh=item.title_zh,
        supplier_name_zh=item.supplier_name_zh,
        price_min_cny=item.price_min_cny,
        price_max_cny=item.price_max_cny,
        price_raw_text=item.price_raw_text,
        moq_raw_text=item.moq_raw_text,
        gallery_image_urls=gallery_urls,
    )


class CatalogBatchService:
    def __init__(self) -> None:
        self.image_downloader = ImageDownloader()
        self.ai_client = OpenAICatalogClient()
        self.renderer = CatalogRenderer()

    def job_dir(self, job_id: uuid.UUID) -> Path:
        path = settings.storage_temporary / str(job_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def update_status(
        self,
        db,
        job_id: uuid.UUID,
        status: str,
        *,
        product_title: str | None = None,
        output_file: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        values: dict = {"status": status, "updated_at": datetime.now(timezone.utc)}
        if product_title is not None:
            values["product_title"] = product_title
        if output_file is not None:
            values["output_file"] = output_file
        if error_code is not None:
            values["error_code"] = error_code
        if error_message is not None:
            values["error_message"] = error_message
        if status in {"completed", "failed"}:
            values["completed_at"] = datetime.now(timezone.utc)
        await db.execute(update(CatalogJob).where(CatalogJob.id == job_id).values(**values))
        await db.commit()

    async def process(
        self,
        db,
        job_id: uuid.UUID,
        status_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> Path:
        job_dir = self.job_dir(job_id)

        async def notify(message: str) -> None:
            if status_callback:
                await status_callback(message)

        result = await db.execute(select(CatalogJob).where(CatalogJob.id == job_id))
        job = result.scalar_one_or_none()
        if not job or not job.products_json:
            raise CatalogBotError("Batch job data is missing")

        try:
            raw_products = json.loads(job.products_json)
            products = [BatchProductInput.model_validate(item) for item in raw_products]
            parsed_products = [batch_input_to_parsed_product(item) for item in products]

            await self.update_status(db, job_id, "downloading_images")
            await notify("Загружаю фотографии…")

            sections: list[BatchCatalogSection] = []
            image_paths_by_index: list[str | None] = []

            for index, product in enumerate(parsed_products):
                images_dir = job_dir / "images" / str(index)
                local_images: list[str] = []
                if product.gallery_image_urls:
                    local_images = await self.image_downloader.download_images(
                        product.gallery_image_urls[:1],
                        [],
                        images_dir,
                        referer=product.source_url,
                    )
                image_paths_by_index.append(local_images[0] if local_images else None)

            await self.update_status(db, job_id, "generating_content")
            await notify("Перевожу и подготавливаю описание…")

            contents = await self._generate_all_content(parsed_products)

            for index, (product, content) in enumerate(zip(parsed_products, contents, strict=True)):
                image_uri = None
                if image_paths_by_index[index]:
                    image_uri = Path(image_paths_by_index[index]).resolve().as_uri()
                sections.append(
                    BatchCatalogSection(
                        content=content,
                        source_url=product.source_url,
                        main_image=image_uri,
                    )
                )

            await self.update_status(db, job_id, "rendering_pdf")
            await notify("Формирую PDF-каталог…")

            collection_title = f"Подборка товаров 1688 ({len(sections)} поз.)"
            pdf_filename = build_batch_pdf_filename(len(sections))
            pdf_path = job_dir / pdf_filename
            await self.renderer.render_batch_pdf(
                sections=sections,
                collection_title=collection_title,
                source_page_url=job.source_url,
                output_path=pdf_path,
            )

            output_dir = settings.storage_output
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / pdf_filename
            if output_path.exists():
                output_path.unlink()
            pdf_path.rename(output_path)

            await self.update_status(
                db,
                job_id,
                "completed",
                product_title=collection_title,
                output_file=str(output_path),
            )
            await notify("Каталог готов.")
            return output_path

        except CatalogBotError:
            raise
        except Exception as exc:
            await self.update_status(
                db,
                job_id,
                "failed",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            logger.exception("catalog_batch_job_failed job_id=%s", job_id)
            raise CatalogBotError(str(exc)) from exc

    async def _generate_all_content(self, products: list[ParsedProduct]) -> list[CatalogContent]:
        semaphore = asyncio.Semaphore(3)

        async def one(product: ParsedProduct) -> CatalogContent:
            async with semaphore:
                try:
                    return await self.ai_client.generate_catalog_content(product)
                except Exception as exc:
                    logger.warning("batch_ai_fallback product=%s error=%s", product.title_zh, exc)
                    return self._fallback_content(product)

        return await asyncio.gather(*(one(product) for product in products))

    def _fallback_content(self, product: ParsedProduct) -> CatalogContent:
        price_display = product.price_raw_text or "Цена уточняется у поставщика."
        if product.price_min_cny is not None:
            if product.price_max_cny and product.price_max_cny != product.price_min_cny:
                price_display = f"¥{product.price_min_cny} – ¥{product.price_max_cny}"
            else:
                price_display = f"¥{product.price_min_cny}"

        return CatalogContent(
            product_name_ru=product.title_zh,
            original_name_zh=product.title_zh,
            short_description_ru="Описание будет уточнено у поставщика.",
            supplier_name=product.supplier_name_zh,
            price_display=price_display,
            moq_display=product.moq_raw_text,
        )

    def cleanup_job(self, job_id: uuid.UUID) -> None:
        job_dir = settings.storage_temporary / str(job_id)
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
