from __future__ import annotations

from pathlib import Path

from app.ai.openai_client import OpenAICatalogClient
from app.catalog.renderer import CatalogRenderer
from app.config import Settings
from app.parser.image_downloader import ImageDownloader
from app.parser.parser_1688 import Parser1688
from app.parser.url_validator import validate_product_url


class CatalogService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.parser = Parser1688(settings)
        self.downloader = ImageDownloader(settings)
        self.ai = OpenAICatalogClient(settings)
        self.renderer = CatalogRenderer(settings)

    async def build_from_url(self, source_url: str, job_dir: Path, status_callback=None) -> Path:
        if status_callback:
            await status_callback("Открываю страницу 1688…")
        validated = await validate_product_url(source_url)
        product = await self.parser.parse(str(validated.final_url), job_dir)
        if status_callback:
            await status_callback("Загружаю фотографии…")
        await self.downloader.download_product_images(product, job_dir)
        if status_callback:
            await status_callback("Перевожу и подготавливаю описание…")
        content = await self.ai.build_catalog_content(product)
        if status_callback:
            await status_callback("Формирую PDF-каталог…")
        pdf = await self.renderer.render_pdf(product, content, job_dir)
        if status_callback:
            await status_callback("Каталог готов.")
        return pdf
