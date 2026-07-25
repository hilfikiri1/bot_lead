from __future__ import annotations

from datetime import date
from pathlib import Path

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from app.ai.schemas import CatalogContent
from app.catalog.models import CatalogRenderData
from app.config import Settings
from app.parser.errors import PdfRenderingError
from app.parser.models import ParsedProduct
from app.utils.filenames import catalog_pdf_filename

logger = structlog.get_logger(__name__)


class CatalogRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.template_dir = Path("app/catalog/templates")
        self.env = Environment(loader=FileSystemLoader(str(self.template_dir)), autoescape=select_autoescape(["html", "xml"]))

    async def render_pdf(self, product: ParsedProduct, content: CatalogContent, job_dir: Path) -> Path:
        job_dir.mkdir(parents=True, exist_ok=True)
        output_pdf = self.settings.output_dir / catalog_pdf_filename(content.product_name_ru)
        html_path = job_dir / "catalog.html"
        logo = str(self.settings.brand_logo_path.resolve()) if self.settings.brand_logo_path.exists() else None
        data = CatalogRenderData(
            brand_name=self.settings.brand_name,
            brand_primary_color=self.settings.brand_primary_color,
            brand_accent_color=self.settings.brand_accent_color,
            brand_text_color=self.settings.brand_text_color,
            brand_logo_path=logo,
            brand_website=self.settings.brand_website or None,
            brand_email=self.settings.brand_email or None,
            brand_phone=self.settings.brand_phone or None,
            content=content,
            product=product,
            image_paths=[str(Path(p).resolve()) for p in product.local_image_paths[: self.settings.max_images]],
            created_date=date.today(),
            source_url=product.source_url,
            output_html=html_path,
            output_pdf=output_pdf,
        )
        html = self.env.get_template("catalog.html").render(data=data, css_path=str(Path("app/catalog/static/catalog.css").resolve()))
        html_path.write_text(html, encoding="utf-8")
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(html_path.resolve().as_uri(), wait_until="domcontentloaded")
                await page.pdf(path=str(output_pdf), format="A4", print_background=True, margin={"top": "14mm", "right": "12mm", "bottom": "14mm", "left": "12mm"})
                await browser.close()
            return output_pdf
        except Exception as exc:
            logger.exception("pdf_rendering_failed", error=str(exc))
            raise PdfRenderingError(str(exc)) from exc
