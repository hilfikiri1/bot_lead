from __future__ import annotations

import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from app.catalog.models import RenderContext
from app.config import settings
from app.exceptions import PdfRenderingError
from app.logging_config import get_logger

logger = get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


class PDFRenderer:
    def __init__(self) -> None:
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
        )

    def _render_html(self, ctx: RenderContext) -> str:
        template = self._jinja_env.get_template("catalog.html")

        # Resolve logo path
        logo_path: str | None = None
        if ctx.logo_path:
            p = Path(ctx.logo_path)
            if p.exists():
                logo_path = f"file://{p.resolve()}"
        
        # Resolve CSS path
        css_path = f"file://{(_STATIC_DIR / 'catalog.css').resolve()}"

        # Convert local image paths to file:// URIs
        image_uris = [f"file://{Path(p).resolve()}" for p in ctx.local_image_paths]

        return template.render(
            brand_name=ctx.brand_name,
            brand_primary_color=ctx.brand_primary_color,
            brand_accent_color=ctx.brand_accent_color,
            brand_text_color=ctx.brand_text_color,
            brand_website=ctx.brand_website,
            brand_email=ctx.brand_email,
            brand_phone=ctx.brand_phone,
            product_name_ru=ctx.product_name_ru,
            original_name_zh=ctx.original_name_zh,
            short_description_ru=ctx.short_description_ru,
            supplier_name=ctx.supplier_name,
            price_display=ctx.price_display,
            price_note=ctx.price_note,
            moq_display=ctx.moq_display,
            price_tiers=ctx.price_tiers,
            specifications=ctx.specifications,
            variants=ctx.variants,
            disclaimer=ctx.disclaimer,
            source_url=ctx.source_url,
            local_image_paths=image_uris,
            created_date=ctx.created_date,
            logo_path=logo_path,
            css_path=css_path,
        )

    async def render(self, ctx: RenderContext, output_path: Path) -> Path:
        """Render catalog HTML and export as A4 PDF via Playwright."""
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            html_content = self._render_html(ctx)
        except Exception as exc:
            raise PdfRenderingError(f"HTML rendering failed: {exc}") from exc

        # Write HTML to a temp file so Playwright can open it with file:// scheme
        with tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp.write(html_content)
            tmp_path = Path(tmp.name)

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--font-render-hinting=medium",
                    ],
                )
                context = await browser.new_context()
                page = await context.new_page()

                file_url = f"file://{tmp_path.resolve()}"
                await page.goto(file_url, wait_until="networkidle", timeout=30_000)
                # Extra wait for fonts and images to render
                await page.wait_for_timeout(1500)

                await page.pdf(
                    path=str(output_path),
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "0mm",
                        "right": "0mm",
                        "bottom": "0mm",
                        "left": "0mm",
                    },
                )
                await browser.close()
        except Exception as exc:
            raise PdfRenderingError(f"PDF generation failed: {exc}") from exc
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise PdfRenderingError("PDF file is empty or was not created")

        logger.info("pdf_rendered", path=str(output_path), size=output_path.stat().st_size)
        return output_path
