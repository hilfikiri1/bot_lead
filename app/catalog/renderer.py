"""PDF catalog renderer using Jinja2 + Playwright."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from app.ai.schemas import CatalogContent
from app.catalog.models import CatalogRenderContext
from app.config import Settings, get_settings
from app.catalog_exceptions import PdfRenderingError
from app.logging_config import get_logger
from app.parser.models import ParsedProduct

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


class CatalogRenderer:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )

    def build_context(
        self,
        content: CatalogContent,
        product: ParsedProduct,
        image_paths: list[str],
    ) -> CatalogRenderContext:
        logo_path = Path(self.settings.brand_logo_path)
        logo_uri = None
        if logo_path.exists():
            logo_uri = logo_path.resolve().as_uri()

        main_image = None
        gallery_images: list[str] = []
        for i, p in enumerate(image_paths):
            uri = Path(p).resolve().as_uri()
            if i == 0:
                main_image = uri
            else:
                gallery_images.append(uri)

        css_path = (STATIC_DIR / "catalog.css").resolve().as_uri()

        return CatalogRenderContext(
            brand_name=self.settings.brand_name,
            brand_primary_color=self.settings.brand_primary_color,
            brand_accent_color=self.settings.brand_accent_color,
            brand_text_color=self.settings.brand_text_color,
            brand_website=self.settings.brand_website,
            brand_email=self.settings.brand_email,
            brand_phone=self.settings.brand_phone,
            logo_path=logo_uri,
            content=content,
            source_url=product.source_url,
            created_date=date.today(),
            main_image=main_image,
            gallery_images=gallery_images,
            css_path=css_path,
        )

    async def render_pdf(
        self,
        content: CatalogContent,
        product: ParsedProduct,
        image_paths: list[str],
        output_path: Path,
    ) -> Path:
        context = self.build_context(content, product, image_paths)
        html = self._render_html(context)

        html_path = output_path.with_suffix(".html")
        html_path.write_text(html, encoding="utf-8")

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox"],
                )
                page = await browser.new_page()
                await page.goto(html_path.resolve().as_uri(), wait_until="domcontentloaded")
                await page.pdf(
                    path=str(output_path),
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "18mm",
                        "bottom": "18mm",
                        "left": "16mm",
                        "right": "16mm",
                    },
                )
                await browser.close()
        except Exception as exc:
            logger.exception("pdf_render_failed", error=str(exc))
            raise PdfRenderingError(str(exc)) from exc

        logger.info("pdf_rendered", path=str(output_path))
        return output_path

    def _render_html(self, context: CatalogRenderContext) -> str:
        template = self.env.get_template("catalog.html")
        return template.render(**context.model_dump())

    def render_html_string(
        self,
        content: CatalogContent,
        product: ParsedProduct,
        image_paths: list[str] | None = None,
    ) -> str:
        ctx = self.build_context(content, product, image_paths or [])
        return self._render_html(ctx)
