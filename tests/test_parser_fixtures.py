from pathlib import Path

from app.parser.parser_1688 import parse_product_from_html


def test_parse_saved_1688_fixture_html() -> None:
    html = Path("tests/fixtures/product_1688_sample.html").read_text(encoding="utf-8")
    product = parse_product_from_html(html, "https://detail.1688.com/offer/123456.html")
    assert product.title_zh == "测试商品标题"
    assert str(product.price_min_cny) == "12.5"
    assert str(product.price_max_cny) == "18.0"
    assert len(product.gallery_image_urls) >= 1
