"""Render a branded PDF catalog from structured content.

Pipeline (as required, OpenAI never creates the PDF):
1. Structured content is injected into a Jinja2 HTML template.
2. Downloaded images (and the logo) are embedded as base64 data URIs.
3. A local HTML file is written into the job's temporary folder.
4. Playwright opens the local HTML and produces an A4 PDF via ``page.pdf()``.
"""

from __future__ import annotations

import base64
import mimetypes
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from app.ai.schemas import CatalogContent
from app.catalog.models import BrandTheme, CatalogRenderContext
from app.config import Settings
from app.exceptions import PdfRenderingError
from app.logging_config import get_logger

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _file_to_data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/jpeg"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _make_qr_data_uri(url: str) -> str | None:
    """Return a QR-code PNG data URI, or ``None`` if qrcode is unavailable."""
    try:
        import qrcode  # imported lazily so the app never crashes without it
    except Exception:  # noqa: BLE001
        return None
    try:
        import io

        img = qrcode.make(url)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to build QR code", error=str(exc))
        return None


class CatalogRenderer:
    """Renders HTML → PDF using Jinja2 and Playwright."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _build_theme(self) -> BrandTheme:
        s = self._settings
        logo_uri = _file_to_data_uri(s.logo_path)
        if logo_uri is None:
            logger.info("Brand logo not found; using text logo", path=str(s.logo_path))
        return BrandTheme(
            name=s.brand_name,
            primary_color=s.brand_primary_color,
            accent_color=s.brand_accent_color,
            text_color=s.brand_text_color,
            website=s.brand_website,
            email=s.brand_email,
            phone=s.brand_phone,
            logo_data_uri=logo_uri,
        )

    def build_context(
        self,
        content: CatalogContent,
        *,
        source_url: str,
        image_paths: list[str],
    ) -> CatalogRenderContext:
        image_uris = [uri for p in image_paths if (uri := _file_to_data_uri(Path(p)))]
        main_image = image_uris[0] if image_uris else None
        gallery = image_uris[1:] if len(image_uris) > 1 else []

        return CatalogRenderContext(
            brand=self._build_theme(),
            content=content,
            source_url=source_url,
            generated_date=date.today().strftime("%d.%m.%Y"),
            main_image=main_image,
            gallery_images=gallery,
            qr_code_data_uri=_make_qr_data_uri(source_url),
        )

    def render_html(self, context: CatalogRenderContext) -> str:
        template = self._env.get_template("catalog.html")
        css = (STATIC_DIR / "catalog.css").read_text(encoding="utf-8")
        return template.render(ctx=context, brand=context.brand, css=css)

    async def render_pdf(
        self,
        content: CatalogContent,
        *,
        source_url: str,
        image_paths: list[str],
        work_dir: Path,
        output_path: Path,
    ) -> Path:
        """Render the catalog and return the path to the generated PDF."""
        try:
            context = self.build_context(
                content, source_url=source_url, image_paths=image_paths
            )
            html = self.render_html(context)

            work_dir.mkdir(parents=True, exist_ok=True)
            html_path = work_dir / "catalog.html"
            html_path.write_text(html, encoding="utf-8")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            await self._html_to_pdf(html_path, output_path)
            return output_path
        except PdfRenderingError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("PDF rendering failed", error=str(exc))
            raise PdfRenderingError(str(exc)) from exc

    async def _html_to_pdf(self, html_path: Path, output_path: Path) -> None:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                page = await browser.new_page()
                await page.goto(html_path.resolve().as_uri(), wait_until="load")
                await page.pdf(
                    path=str(output_path),
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "14mm",
                        "bottom": "14mm",
                        "left": "12mm",
                        "right": "12mm",
                    },
                )
            finally:
                await browser.close()
