"""1688 catalog generation service integrated with bot_lead."""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update

from app.ai.openai_client import OpenAICatalogClient
from app.catalog.renderer import CatalogRenderer
from app.catalog_exceptions import CatalogBotError
from app.config import get_settings
from app.models.catalog_job import CatalogJob
from app.parser.image_downloader import ImageDownloader
from app.parser.parser_1688 import Parser1688
from app.parser.url_validator import resolve_and_validate_url
from app.utils.filenames import build_pdf_filename

logger = logging.getLogger(__name__)
settings = get_settings()


class CatalogService:
    def __init__(self) -> None:
        self.parser = Parser1688()
        self.image_downloader = ImageDownloader()
        self.ai_client = OpenAICatalogClient()
        self.renderer = CatalogRenderer()

    def job_dir(self, job_id: uuid.UUID) -> Path:
        path = settings.storage_temporary / str(job_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def get_active_job(self, db, telegram_user_id: int) -> CatalogJob | None:
        result = await db.execute(
            select(CatalogJob)
            .where(CatalogJob.telegram_user_id == telegram_user_id)
            .where(CatalogJob.status.notin_(["completed", "failed"]))
            .order_by(CatalogJob.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_job(self, db, telegram_user_id: int, telegram_chat_id: int, source_url: str) -> CatalogJob:
        job = CatalogJob(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            source_url=source_url,
            status="received",
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job

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
        source_url: str,
        status_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> Path:
        job_dir = self.job_dir(job_id)

        async def notify(message: str) -> None:
            if status_callback:
                await status_callback(message)

        try:
            await self.update_status(db, job_id, "validating")
            await notify("Открываю страницу 1688…")
            validated_url = await resolve_and_validate_url(source_url)

            await self.update_status(db, job_id, "parsing")
            product = await self.parser.parse(validated_url, debug_dir=job_dir)

            await self.update_status(db, job_id, "downloading_images")
            await notify("Загружаю фотографии…")
            images_dir = job_dir / "images"
            local_images = await self.image_downloader.download_images(
                product.gallery_image_urls,
                product.detail_image_urls,
                images_dir,
                referer=validated_url,
            )
            product.local_image_paths = local_images

            await self.update_status(db, job_id, "generating_content")
            await notify("Перевожу и подготавливаю описание…")
            catalog_content = await self.ai_client.generate_catalog_content(product)

            await self.update_status(db, job_id, "rendering_pdf")
            await notify("Формирую PDF-каталог…")
            pdf_filename = build_pdf_filename(catalog_content.product_name_ru)
            pdf_path = job_dir / pdf_filename
            await self.renderer.render_pdf(catalog_content, product, local_images, pdf_path)

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
                product_title=catalog_content.product_name_ru,
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
            logger.exception("catalog_job_failed job_id=%s", job_id)
            raise CatalogBotError(str(exc)) from exc

    def cleanup_job(self, job_id: uuid.UUID) -> None:
        job_dir = settings.storage_temporary / str(job_id)
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)

    def cleanup_expired_pdfs(self) -> int:
        output_dir = settings.storage_output
        if not output_dir.exists():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.catalog_pdf_retention_hours)
        removed = 0
        for path in output_dir.glob("*.pdf"):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
