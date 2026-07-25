from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from app.catalog.models import CatalogRenderInput
from app.exceptions import PdfRenderingError


class CatalogRenderer:
    def __init__(self) -> None:
        self._templates_dir = Path("app/catalog/templates")
        self._static_dir = Path("app/catalog/static")
        self._jinja = Environment(
            loader=FileSystemLoader(self._templates_dir),
            autoescape=select_autoescape(enabled_extensions=("html",)),
        )

    async def render(self, payload: CatalogRenderInput, output_pdf_path: Path, temp_dir: Path) -> Path:
        temp_dir.mkdir(parents=True, exist_ok=True)
        output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

        css_path = self._static_dir / "catalog.css"
        template = self._jinja.get_template("catalog.html")
        html = template.render(
            content=payload.content,
            source_url=payload.source_url,
            image_paths=[Path(p).absolute().as_uri() for p in payload.image_paths],
            generated_date=payload.generated_date.isoformat(),
            brand_name=payload.brand_name,
            brand_primary_color=payload.brand_primary_color,
            brand_accent_color=payload.brand_accent_color,
            brand_text_color=payload.brand_text_color,
            logo_path=(Path(payload.logo_path).absolute().as_uri() if payload.logo_path and Path(payload.logo_path).exists() else None),
            css_path=css_path.absolute().as_uri(),
        )
        html_path = temp_dir / "catalog.html"
        html_path.write_text(html, encoding="utf-8")

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(html_path.absolute().as_uri(), wait_until="domcontentloaded")
                await page.pdf(
                    path=str(output_pdf_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
                )
                await browser.close()
        except Exception as exc:
            raise PdfRenderingError("Failed to render PDF") from exc

        return output_pdf_path
