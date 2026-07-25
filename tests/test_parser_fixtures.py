"""Tests for parser HTML fixtures."""

from decimal import Decimal

from app.parser.parser_1688 import parse_html_fixture
from app.utils.filenames import build_pdf_filename, safe_filename
from app.utils.images import dedupe_image_hashes
from pathlib import Path


FIXTURE_HTML = """
<html>
<head><title>测试商品页面</title></head>
<body>
  <h1>高品质工业阀门</h1>
  <div class="price">¥ 45.50</div>
  <img src="https://cbu01.alicdn.com/img/test1.jpg" />
  <img src="https://cbu01.alicdn.com/img/test2.png" />
  <img src="https://cbu01.alicdn.com/img/test1.jpg" />
</body>
</html>
"""


class TestParserFixtures:
    def test_parse_html_fixture(self):
        product = parse_html_fixture(FIXTURE_HTML)
        assert product.title_zh == "高品质工业阀门"
        assert product.price_min_cny == Decimal("45.50")
        assert len(product.gallery_image_urls) >= 1

    def test_safe_filename(self):
        assert safe_filename("Тестовый товар!") == "catalog" or "___" in safe_filename("!!!")
        name = safe_filename("Industrial Valve 304")
        assert "Industrial" in name

    def test_build_pdf_filename(self):
        from datetime import date

        name = build_pdf_filename("Test Product", created=date(2026, 7, 25))
        assert name.startswith("Babrik_Solutions_")
        assert name.endswith("2026-07-25.pdf")

    def test_dedupe_image_hashes(self, tmp_path: Path):
        f1 = tmp_path / "a.jpg"
        f2 = tmp_path / "b.jpg"
        f1.write_bytes(b"same content")
        f2.write_bytes(b"same content")
        f3 = tmp_path / "c.jpg"
        f3.write_bytes(b"different")
        result = dedupe_image_hashes([f1, f2, f3])
        assert len(result) == 2
