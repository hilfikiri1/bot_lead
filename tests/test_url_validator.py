"""Tests for URL validator — no live network calls required."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.exceptions import InvalidProductUrlError, UnsupportedDomainError
from app.parser.url_validator import URLValidator, _domain_is_allowed, _is_private_ip


# ── Unit tests for helper functions ──────────────────────────────────────────

class TestDomainAllowlist:
    def test_exact_domain_allowed(self):
        assert _domain_is_allowed("1688.com") is True

    def test_subdomain_allowed(self):
        assert _domain_is_allowed("detail.1688.com") is True
        assert _domain_is_allowed("www.1688.com") is True
        assert _domain_is_allowed("m.1688.com") is True

    def test_similar_domain_rejected(self):
        assert _domain_is_allowed("fake1688.com") is False
        assert _domain_is_allowed("1688.com.evil.com") is False
        assert _domain_is_allowed("evil.com") is False
        assert _domain_is_allowed("alibaba.com") is False


class TestPrivateIPDetection:
    def test_loopback(self):
        assert _is_private_ip("127.0.0.1") is True
        assert _is_private_ip("127.255.255.255") is True

    def test_private_ranges(self):
        assert _is_private_ip("10.0.0.1") is True
        assert _is_private_ip("192.168.1.100") is True
        assert _is_private_ip("172.16.0.1") is True

    def test_public_ip_allowed(self):
        assert _is_private_ip("8.8.8.8") is False
        assert _is_private_ip("1.1.1.1") is False

    def test_ipv6_loopback(self):
        assert _is_private_ip("::1") is True

    def test_not_an_ip(self):
        # Hostnames are not IPs, should return False
        assert _is_private_ip("detail.1688.com") is False


# ── Integration tests for URLValidator (no real HTTP) ────────────────────────

class TestURLValidator:
    def setup_method(self):
        # Disable redirect following in tests
        self.validator = URLValidator(follow_redirects=False)

    @pytest.mark.asyncio
    async def test_valid_1688_url(self):
        url = "https://detail.1688.com/offer/123456789.html"
        result = await self.validator.validate(url)
        assert result == url

    @pytest.mark.asyncio
    async def test_valid_www_1688_url(self):
        url = "https://www.1688.com/page/some-product"
        result = await self.validator.validate(url)
        assert result == url

    @pytest.mark.asyncio
    async def test_http_rejected(self):
        with pytest.raises(InvalidProductUrlError):
            await self.validator.validate("http://detail.1688.com/offer/123.html")

    @pytest.mark.asyncio
    async def test_wrong_domain(self):
        with pytest.raises(UnsupportedDomainError):
            await self.validator.validate("https://www.taobao.com/product/123")

    @pytest.mark.asyncio
    async def test_localhost_rejected(self):
        with pytest.raises(InvalidProductUrlError):
            await self.validator.validate("https://localhost/offer/123")

    @pytest.mark.asyncio
    async def test_private_ip_rejected(self):
        with pytest.raises(InvalidProductUrlError):
            await self.validator.validate("https://192.168.1.1/offer/123")

    @pytest.mark.asyncio
    async def test_url_too_long(self):
        long_url = "https://detail.1688.com/" + "a" * 2100
        with pytest.raises(InvalidProductUrlError):
            await self.validator.validate(long_url)

    @pytest.mark.asyncio
    async def test_ftp_rejected(self):
        with pytest.raises(InvalidProductUrlError):
            await self.validator.validate("ftp://detail.1688.com/file.txt")

    @pytest.mark.asyncio
    async def test_file_scheme_rejected(self):
        with pytest.raises(InvalidProductUrlError):
            await self.validator.validate("file:///etc/passwd")

    @pytest.mark.asyncio
    async def test_no_scheme_rejected(self):
        with pytest.raises(InvalidProductUrlError):
            await self.validator.validate("detail.1688.com/offer/123")

    @pytest.mark.asyncio
    async def test_evil_lookalike_domain(self):
        with pytest.raises(UnsupportedDomainError):
            await self.validator.validate("https://1688.com.phishing.com/product")

    @pytest.mark.asyncio
    async def test_subdomain_of_1688_allowed(self):
        url = "https://s.1688.com/some/path"
        result = await self.validator.validate(url)
        assert result == url
