"""Catalog generation orchestration service."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.ai.openai_client import OpenAICatalogClient
from app.catalog.renderer import CatalogRenderer
from app.config import Settings, get_settings
from app.database.models import JobStatus
from app.database.repositories import CatalogJobRepository
from app.logging_config import get_logger
from app.parser.image_downloader import ImageDownloader
from app.parser.parser_1688 import Parser1688
from app.parser.url_validator import resolve_and_validate_url
from app.utils.filenames import build_pdf_filename

logger = get_logger(__name__)


class CatalogService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.parser = Parser1688(settings=self.settings)
        self.image_downloader = ImageDownloader(settings=self.settings)
        self.ai_client = OpenAICatalogClient(settings=self.settings)
        self.renderer = CatalogRenderer(settings=self.settings)

    def job_dir(self, job_id: uuid.UUID) -> Path:
        return self.settings.storage_temporary / str(job_id)

    async def process(
        self,
        job_id: uuid.UUID,
        source_url: str,
        repo: CatalogJobRepository,
        status_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> Path:
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)

        async def update_status(status: JobStatus, message: str | None = None) -> None:
            await repo.update_status(job_id, status)
            if status_callback and message:
                await status_callback(message)

        try:
            await update_status(JobStatus.VALIDATING, "Открываю страницу 1688…")
            validated_url = await resolve_and_validate_url(source_url)

            await update_status(JobStatus.PARSING, "Открываю страницу 1688…")
            product = await self.parser.parse(validated_url, debug_dir=job_dir)

            await update_status(JobStatus.DOWNLOADING_IMAGES, "Загружаю фотографии…")
            images_dir = job_dir / "images"
            local_images = await self.image_downloader.download_images(
                product.gallery_image_urls,
                product.detail_image_urls,
                images_dir,
                referer=validated_url,
            )
            product.local_image_paths = local_images

            parsed_json_path = job_dir / "parsed_product.json"
            parsed_json_path.write_text(
                product.model_dump_json(indent=2),
                encoding="utf-8",
            )

            await update_status(JobStatus.GENERATING_CONTENT, "Перевожу и подготавливаю описание…")
            catalog_content = await self.ai_client.generate_catalog_content(product)

            content_json_path = job_dir / "catalog_content.json"
            content_json_path.write_text(
                catalog_content.model_dump_json(indent=2),
                encoding="utf-8",
            )

            await update_status(JobStatus.RENDERING_PDF, "Формирую PDF-каталог…")
            pdf_filename = build_pdf_filename(catalog_content.product_name_ru)
            pdf_path = job_dir / pdf_filename

            await self.renderer.render_pdf(
                catalog_content,
                product,
                local_images,
                pdf_path,
            )

            output_path = self.settings.storage_output / pdf_filename
            output_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.rename(output_path)

            await repo.update_status(
                job_id,
                JobStatus.COMPLETED,
                product_title=catalog_content.product_name_ru,
                output_file=str(output_path),
            )

            if status_callback:
                await status_callback("Каталог готов.")

            return output_path

        except Exception as exc:
            error_code = type(exc).__name__
            error_message = str(exc)
            user_message = getattr(exc, "user_message", "Временная ошибка")
            await repo.update_status(
                job_id,
                JobStatus.FAILED,
                error_code=error_code,
                error_message=error_message,
            )
            logger.exception("catalog_job_failed", job_id=str(job_id), error=error_message)
            raise exc
