"""Manual visual check: render a full sample catalog PDF + a PNG preview.

Not part of the automated test suite. Usage:
    python scripts/render_sample.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from app.ai.schemas import (  # noqa: E402
    CatalogContent,
    CatalogPriceTier,
    CatalogSpecification,
)
from app.catalog.renderer import CatalogRenderer  # noqa: E402
from app.config import get_settings  # noqa: E402


def _make_images(work: Path) -> list[str]:
    paths = []
    colors = [(210, 150, 60), (40, 90, 150), (90, 150, 90), (150, 60, 90)]
    for i, color in enumerate(colors):
        img = Image.new("RGB", (700, 700), color)
        p = work / f"img_{i}.jpg"
        img.save(p, format="JPEG")
        paths.append(str(p))
    return paths


async def main() -> None:
    settings = get_settings()
    work = Path("storage/temporary/_sample")
    work.mkdir(parents=True, exist_ok=True)
    out = Path("storage/output/_sample.pdf")

    content = CatalogContent(
        product_name_ru="Термокружка из нержавеющей стали 500 мл",
        original_name_zh="高品质不锈钢保温杯 双层真空 便携水杯",
        short_description_ru=(
            "Двухслойная вакуумная термокружка объёмом 500 мл. "
            "Корпус из нержавеющей стали. Доступно несколько цветов."
        ),
        supplier_name="义乌市优质日用品有限公司",
        price_display="15.50–22.00 CNY",
        price_note=None,
        moq_display="от 2 шт.",
        price_tiers=[
            CatalogPriceTier(quantity="от 2 шт.", price="22.00 CNY"),
            CatalogPriceTier(quantity="от 100 шт.", price="18.50 CNY"),
            CatalogPriceTier(quantity="от 1000 шт.", price="15.50 CNY"),
        ],
        specifications=[
            CatalogSpecification(name="Материал", value="Нержавеющая сталь 304"),
            CatalogSpecification(name="Объём", value="500 мл"),
        ],
        variants=[CatalogSpecification(name="Цвет", value="серебристый/чёрный/синий")],
        disclaimer=(
            "Информация в каталоге сформирована автоматически на основании данных "
            "поставщика на платформе 1688.com."
        ),
    )

    renderer = CatalogRenderer(settings)
    images = _make_images(work)
    pdf = await renderer.render_pdf(
        content,
        source_url="https://detail.1688.com/offer/123456789.html",
        image_paths=images,
        work_dir=work,
        output_path=out,
    )
    print(f"PDF written: {pdf} ({pdf.stat().st_size} bytes)")

    # Render the cover to PNG for a quick visual sanity check.
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 900, "height": 1200})
        await page.goto((work / "catalog.html").resolve().as_uri(), wait_until="load")
        await page.screenshot(path="storage/output/_sample_cover.png")
        await browser.close()
    print("PNG preview: storage/output/_sample_cover.png")


if __name__ == "__main__":
    asyncio.run(main())
