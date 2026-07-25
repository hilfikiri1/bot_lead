from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from app.ai.schemas import CatalogContent
from app.catalog.models import CatalogRenderInput
from app.catalog.renderer import CatalogRenderer
from app.utils.images import content_sha256


class _FakePage:
    async def goto(self, *_args, **_kwargs):
        return None

    async def pdf(self, path: str, **_kwargs):
        Path(path).write_bytes(b"%PDF-1.4\n%fake\n")


class _FakeBrowser:
    async def new_page(self):
        return _FakePage()

    async def close(self):
        return None


class _FakeChromium:
    async def launch(self, **_kwargs):
        return _FakeBrowser()


class _FakePlaywright:
    chromium = _FakeChromium()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


@pytest.mark.asyncio
async def test_catalog_renderer_creates_pdf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.catalog.renderer.async_playwright", lambda: _FakePlaywright())

    img_path = tmp_path / "img.jpg"
    Image.new("RGB", (800, 600), color=(200, 200, 200)).save(img_path)

    content = CatalogContent(
        product_name_ru="Товар",
        original_name_zh="测试商品",
        short_description_ru="Краткое описание.",
        supplier_name=None,
        price_display="10 CNY",
        price_note=None,
        moq_display="100 шт.",
        price_tiers=[],
        specifications=[],
        variants=[],
        disclaimer="Дисклеймер",
    )
    payload = CatalogRenderInput(
        content=content,
        source_url="https://detail.1688.com/offer/1.html",
        image_paths=[str(img_path)],
        generated_date=date.today(),
        brand_name="Babrik Solutions",
        brand_primary_color="#0B1F3A",
        brand_accent_color="#D8A34A",
        brand_text_color="#20242A",
        logo_path=None,
    )
    renderer = CatalogRenderer()
    out_pdf = tmp_path / "out.pdf"
    rendered = await renderer.render(payload, out_pdf, tmp_path / "tmp")
    assert rendered.exists()
    assert rendered.read_bytes().startswith(b"%PDF")


def test_image_hash_dedup_logic() -> None:
    a = b"same-image-content"
    b = b"same-image-content"
    c = b"other-image-content"
    hashes = {content_sha256(a), content_sha256(b), content_sha256(c)}
    assert len(hashes) == 2
