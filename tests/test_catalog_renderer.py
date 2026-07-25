from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from app.ai.schemas import CatalogContent, CatalogPriceTier, CatalogSpecification
from app.catalog.renderer import CatalogRenderer
from app.config import Settings
from app.parser.models import ParsedProduct


@pytest.mark.asyncio
async def test_catalog_renderer_creates_pdf(tmp_path: Path):
    image_path = tmp_path / "product.jpg"
    Image.new("RGB", (800, 800), color=(240, 240, 240)).save(image_path)
    settings = Settings(output_dir=tmp_path / "output", temporary_dir=tmp_path / "temp", brand_logo_path=tmp_path / "missing.png")
    settings.ensure_directories()
    product = ParsedProduct(source_url="https://detail.1688.com/offer/1.html", title_zh="保温杯", price_min_cny=Decimal("12.5"), gallery_image_urls=["https://cbu01.alicdn.com/a.jpg"], local_image_paths=[str(image_path)])
    content = CatalogContent(product_name_ru="Термокружка", original_name_zh="保温杯", short_description_ru="Товар для тестового каталога.", price_display="12.5 CNY", price_tiers=[CatalogPriceTier(quantity="20 шт.", price="12.5 CNY")], specifications=[CatalogSpecification(name="Материал", value="уточняется у поставщика")])
    pdf = await CatalogRenderer(settings).render_pdf(product, content, tmp_path / "job")
    assert pdf.exists()
    assert pdf.stat().st_size > 1000
