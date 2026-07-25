"""Tests for URL validator."""

import pytest

from app.exceptions import InvalidProductUrlError, UnsupportedDomainError
from app.parser.url_validator import normalize_1688_url, validate_url_format


class TestValidateUrlFormat:
    def test_valid_detail_url(self):
        url = "https://detail.1688.com/offer/1234567890.html"
        assert validate_url_format(url) == url

    def test_valid_offer_url(self):
        url = "https://www.1688.com/offer/9876543210.html"
        assert validate_url_format(url) == url

    def test_reject_other_domain(self):
        with pytest.raises(UnsupportedDomainError):
            validate_url_format("https://www.taobao.com/item/123")

    def test_reject_http(self):
        with pytest.raises(UnsupportedDomainError):
            validate_url_format("http://detail.1688.com/offer/123.html")

    def test_reject_localhost(self):
        with pytest.raises(UnsupportedDomainError):
            validate_url_format("https://localhost/offer/123")

    def test_reject_private_ip(self):
        with pytest.raises(UnsupportedDomainError):
            validate_url_format("https://192.168.1.1/offer/123")

    def test_reject_file_protocol(self):
        with pytest.raises(UnsupportedDomainError):
            validate_url_format("file:///etc/passwd")

    def test_reject_non_product_path(self):
        with pytest.raises(InvalidProductUrlError):
            validate_url_format("https://www.1688.com/")

    def test_reject_too_long_url(self):
        with pytest.raises(InvalidProductUrlError):
            validate_url_format("https://detail.1688.com/offer/1" + "a" * 3000)

    def test_normalize(self):
        url = "https://detail.1688.com/offer/123.html"
        assert normalize_1688_url(url) == url
