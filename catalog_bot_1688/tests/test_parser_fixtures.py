"""Parse a saved 1688 HTML fixture without touching the live site.

These tests exercise Layer 1 (embedded JSON) extraction only, which operates on
the raw HTML string and therefore needs no browser.
"""

from __future__ import annotations

from decimal import Decimal

from app.parser.parser_1688 import Parser1688


def test_extract_json_blobs(settings, fixtures_dir) -> None:
    html = (fixtures_dir / "product_page.html").read_text(encoding="utf-8")
    parser = Parser1688(settings)
    blobs = parser._collect_json_blobs(html, [])
    assert blobs, "expected at least one JSON blob from embedded state + JSON-LD"


def test_extract_title(settings, fixtures_dir) -> None:
    html = (fixtures_dir / "product_page.html").read_text(encoding="utf-8")
    parser = Parser1688(settings)
    blobs = parser._collect_json_blobs(html, [])
    title = parser._extract_title(blobs)
    assert title is not None
    assert "保温杯" in title


def test_extract_supplier(settings, fixtures_dir) -> None:
    html = (fixtures_dir / "product_page.html").read_text(encoding="utf-8")
    parser = Parser1688(settings)
    blobs = parser._collect_json_blobs(html, [])
    supplier = parser._extract_supplier(blobs)
    assert supplier == "义乌市优质日用品有限公司"


def test_extract_price(settings, fixtures_dir) -> None:
    html = (fixtures_dir / "product_page.html").read_text(encoding="utf-8")
    parser = Parser1688(settings)
    blobs = parser._collect_json_blobs(html, [])
    low, high, raw = parser._extract_price(blobs)
    assert low == Decimal("15.50")
    assert high == Decimal("22.00")
    assert raw is not None


def test_extract_specifications(settings, fixtures_dir) -> None:
    html = (fixtures_dir / "product_page.html").read_text(encoding="utf-8")
    parser = Parser1688(settings)
    blobs = parser._collect_json_blobs(html, [])
    specs = parser._extract_specs(blobs)
    names = {spec.name_zh for spec in specs}
    assert "材质" in names
    assert "容量" in names


def test_extract_gallery(settings, fixtures_dir) -> None:
    html = (fixtures_dir / "product_page.html").read_text(encoding="utf-8")
    parser = Parser1688(settings)
    blobs = parser._collect_json_blobs(html, [])
    images = parser._extract_gallery(blobs, "https://detail.1688.com/offer/1.html")
    assert any("product1_800x800.jpg" in url for url in images)
    assert all(url.startswith("https://") for url in images)


def test_extract_tiers(settings, fixtures_dir) -> None:
    html = (fixtures_dir / "product_page.html").read_text(encoding="utf-8")
    parser = Parser1688(settings)
    blobs = parser._collect_json_blobs(html, [])
    tiers = parser._extract_tiers(blobs)
    assert len(tiers) >= 1
    assert all(tier.price_cny is not None for tier in tiers)
