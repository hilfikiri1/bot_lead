"""Filename normalization tests."""

from __future__ import annotations

import re
from datetime import date

from app.utils.filenames import build_catalog_filename, safe_asset_name, slugify

SAFE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def test_slugify_ascii() -> None:
    assert slugify("Stainless Steel Bottle") == "Stainless_Steel_Bottle"


def test_slugify_non_ascii_falls_back() -> None:
    # Chinese / Cyrillic characters are dropped -> default is used.
    assert slugify("不锈钢保温杯", default="product") == "product"
    assert slugify("Термокружка", default="product") == "product"


def test_slugify_mixed() -> None:
    result = slugify("Bottle 500ml 不锈钢")
    assert SAFE_RE.match(result)
    assert "500ml" in result


def test_build_catalog_filename() -> None:
    name = build_catalog_filename(
        "Babrik Solutions", "Stainless Bottle", when=date(2026, 7, 25)
    )
    assert name == "Babrik_Solutions_Stainless_Bottle_2026-07-25.pdf"
    assert SAFE_RE.match(name)


def test_build_catalog_filename_non_ascii_product() -> None:
    name = build_catalog_filename("Babrik Solutions", "不锈钢保温杯", when=date(2026, 7, 25))
    assert name.startswith("Babrik_Solutions_")
    assert name.endswith("2026-07-25.pdf")
    assert SAFE_RE.match(name)


def test_safe_asset_name() -> None:
    assert safe_asset_name("gallery", 3) == "gallery_003.jpg"
    assert SAFE_RE.match(safe_asset_name("详情", 1, "png"))
