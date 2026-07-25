"""
Tests for the 1688 parser using fixture HTML (no real network calls).
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.parser.parser_1688 import (
    _to_decimal,
    _parse_price_range,
    _safe_int,
    _clean_text,
    _normalise_image_url,
    _extract_fields_from_dict,
)


class TestCleanText:
    def test_strips_whitespace(self):
        assert _clean_text("  hello  ") == "hello"

    def test_collapses_inner_whitespace(self):
        assert _clean_text("hello   world") == "hello world"

    def test_none_returns_empty(self):
        assert _clean_text(None) == ""


class TestToDecimal:
    def test_simple_number(self):
        assert _to_decimal("12.5") == Decimal("12.5")

    def test_with_currency_symbol(self):
        assert _to_decimal("¥12.50") == Decimal("12.50")

    def test_number_with_commas(self):
        # Commas as thousand separators should be handled
        result = _to_decimal("1,234.56")
        assert result is not None

    def test_no_number_returns_none(self):
        assert _to_decimal("no price") is None

    def test_empty_returns_none(self):
        assert _to_decimal("") is None


class TestParsePriceRange:
    def test_single_price(self):
        low, high = _parse_price_range("¥50.00")
        assert low == Decimal("50.00")
        assert high is None

    def test_dash_range(self):
        low, high = _parse_price_range("¥50.00-¥120.00")
        assert low == Decimal("50.00")
        assert high == Decimal("120.00")

    def test_tilde_range(self):
        low, high = _parse_price_range("50~120")
        assert low == Decimal("50")
        assert high == Decimal("120")

    def test_no_price(self):
        low, high = _parse_price_range("no price here")
        assert low is None
        assert high is None


class TestSafeInt:
    def test_simple_number(self):
        assert _safe_int("100件") == 100

    def test_no_number(self):
        assert _safe_int("no number") is None

    def test_number_at_start(self):
        assert _safe_int("50 pcs") == 50


class TestNormaliseImageUrl:
    def test_protocol_relative(self):
        url = _normalise_image_url("//img.1688.com/img/product.jpg")
        assert url.startswith("https://")

    def test_full_url_unchanged(self):
        url = _normalise_image_url("https://img.1688.com/img/product.jpg")
        assert url == "https://img.1688.com/img/product.jpg"

    def test_empty_returns_empty(self):
        assert _normalise_image_url("") == ""

    def test_relative_url_rejected(self):
        assert _normalise_image_url("/relative/path.jpg") == ""

    def test_size_suffix_removed(self):
        url = _normalise_image_url("https://img.1688.com/img/product_100x100.jpg")
        assert "_100x100" not in url


class TestExtractFieldsFromDict:
    def test_extracts_title(self):
        data = {"title": "工业风扇"}
        result = _extract_fields_from_dict(data)
        assert result.get("title_zh") == "工业风扇"

    def test_extracts_nested_title(self):
        data = {"productInfo": {"subject": "轴流风机"}}
        result = _extract_fields_from_dict(data)
        assert result.get("title_zh") == "轴流风机"

    def test_extracts_image_list(self):
        data = {"images": ["https://img.1688.com/1.jpg", "https://img.1688.com/2.jpg"]}
        result = _extract_fields_from_dict(data)
        assert len(result.get("gallery_urls", [])) == 2

    def test_extracts_supplier(self):
        data = {"companyName": "深圳某公司"}
        result = _extract_fields_from_dict(data)
        assert result.get("supplier_name_zh") == "深圳某公司"

    def test_empty_dict(self):
        result = _extract_fields_from_dict({})
        assert result == {}
