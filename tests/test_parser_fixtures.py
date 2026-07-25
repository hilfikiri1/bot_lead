from pathlib import Path

from app.config import Settings
from app.parser.parser_1688 import Parser1688


def test_parser_extracts_fixture_product():
    html = Path("tests/fixtures/product_1688.html").read_text(encoding="utf-8")
    product = Parser1688(Settings()).parse_html(html, "https://detail.1688.com/offer/123.html")
    assert product.title_zh == "不锈钢保温杯 500ml"
    assert product.supplier_name_zh == "义乌市测试供应商"
    assert product.moq == 20
    assert product.gallery_image_urls
    assert product.specifications[0].name_zh == "材质"
