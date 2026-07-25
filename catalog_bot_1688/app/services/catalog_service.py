"""End-to-end catalog generation pipeline (no Telegram / DB concerns here).

Given a source URL and a per-job working directory, this service:
1. validates + resolves the URL (SSRF-safe, re-checked after redirects);
2. opens the page with Playwright (saved 1688 session);
3. parses product data (multi-layer);
4. downloads + processes images;
5. asks OpenAI for structured Russian content;
6. renders a branded A4 PDF locally.

It reports progress through an async status callback so the bot can edit a single
status message. Errors are surfaced as :class:`app.exceptions.CatalogError`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.ai.openai_client import OpenAICatalogClient
from app.ai.prompts import PRICE_UNKNOWN_TEXT
from app.catalog.renderer import CatalogRenderer
from app.config import Settings
from app.database.models import JobStatus
from app.exceptions import CatalogError, ProductDataNotFoundError
from app.logging_config import get_logger
from app.parser.browser import BrowserManager
from app.parser.image_downloader import ImageDownloader
from app.parser.models import ParsedProduct
from app.parser.parser_1688 import Parser1688
from app.parser.url_validator import resolve_and_validate
from app.utils.filenames import build_catalog_filename

logger = get_logger(__name__)

StatusCallback = Callable[[JobStatus], Awaitable[None]]


@dataclass
class CatalogResult:
    pdf_path: Path
    product_title_ru: str
    short_name: str


async def _noop_status(_: JobStatus) -> None:
    return None


class CatalogService:
    """Orchestrates the full pipeline for a single product link."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._renderer = CatalogRenderer(settings)

    async def build_catalog(
        self,
        source_url: str,
        *,
        work_dir: Path,
        output_dir: Path,
        on_status: StatusCallback | None = None,
    ) -> CatalogResult:
        on_status = on_status or _noop_status
        work_dir.mkdir(parents=True, exist_ok=True)
        debug_dir = work_dir / "debug"

        # 1. Validate + resolve URL.
        await on_status(JobStatus.VALIDATING)
        final_url = await resolve_and_validate(source_url)

        # 2 + 3. Open page & parse.
        await on_status(JobStatus.PARSING)
        product, cookies = await self._parse(final_url, debug_dir)

        # 4. Download images.
        await on_status(JobStatus.DOWNLOADING_IMAGES)
        product = await self._download_images(product, final_url, work_dir, cookies)

        # 5. OpenAI structured content.
        await on_status(JobStatus.GENERATING_CONTENT)
        content = await self._generate_content(product)

        # 6. Render PDF.
        await on_status(JobStatus.RENDERING_PDF)
        pdf_path = await self._render(product, content, work_dir, output_dir)

        return CatalogResult(
            pdf_path=pdf_path,
            product_title_ru=content.product_name_ru,
            short_name=content.product_name_ru[:40],
        )

    async def _parse(
        self, url: str, debug_dir: Path
    ) -> tuple[ParsedProduct, list[dict]]:
        parser = Parser1688(self._settings)
        async with BrowserManager(self._settings) as browser:
            async with browser.product_page(url, debug_dir=debug_dir) as ready:
                product = await parser.parse(ready, url)
                cookies = ready.cookies
        return product, cookies

    async def _download_images(
        self, product: ParsedProduct, referer: str, work_dir: Path, cookies: list[dict]
    ) -> ParsedProduct:
        downloader = ImageDownloader(self._settings, cookies=cookies)
        images_dir = work_dir / "images"
        result = await downloader.download(
            product.gallery_image_urls,
            product.detail_image_urls,
            referer=referer,
            destination=images_dir,
        )
        product.local_image_paths = result.all_paths
        if not product.local_image_paths:
            # Minimum viable output requires at least one image.
            raise ProductDataNotFoundError(
                "No downloadable images",
                user_message=(
                    "Не удалось загрузить фотографии товара. "
                    "Возможно, 1688 ограничил доступ к странице."
                ),
            )
        return product

    async def _generate_content(self, product: ParsedProduct):
        client = OpenAICatalogClient(self._settings)
        main_image = product.local_image_paths[0] if product.local_image_paths else None
        content = await client.generate(product, main_image_path=main_image)
        if not content.price_display.strip():
            content.price_display = PRICE_UNKNOWN_TEXT
        return content

    async def _render(
        self,
        product: ParsedProduct,
        content,
        work_dir: Path,
        output_dir: Path,
    ) -> Path:
        filename = build_catalog_filename(
            self._settings.brand_name, content.product_name_ru
        )
        output_path = output_dir / filename
        try:
            return await self._renderer.render_pdf(
                content,
                source_url=product.source_url,
                image_paths=product.local_image_paths,
                work_dir=work_dir,
                output_path=output_path,
            )
        except CatalogError:
            raise
