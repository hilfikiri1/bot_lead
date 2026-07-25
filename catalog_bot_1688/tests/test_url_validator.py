"""URL validation / SSRF protection tests."""

from __future__ import annotations

import pytest

from app.exceptions import InvalidProductUrlError, UnsupportedDomainError
from app.parser.url_validator import (
    MAX_URL_LENGTH,
    is_allowed_domain,
    validate_url_syntax,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://detail.1688.com/offer/123456789.html",
        "https://m.1688.com/offer/abc.html",
        "https://www.1688.com/offer/1.html",
        "https://1688.com/offer/1.html",
        "https://qr.1688.com/share/xyz",
    ],
)
def test_allowed_urls(url: str) -> None:
    assert validate_url_syntax(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/offer/1.html",
        "https://detail.taobao.com/item.htm",
        "https://1688.com.evil.com/offer/1.html",
        "https://evil1688.com/offer/1.html",
    ],
)
def test_rejects_foreign_domains(url: str) -> None:
    with pytest.raises(UnsupportedDomainError):
        validate_url_syntax(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://detail.1688.com/offer/1.html",  # not https
        "file:///etc/passwd",
        "ftp://detail.1688.com/x",
        "gopher://detail.1688.com/x",
    ],
)
def test_rejects_non_https_schemes(url: str) -> None:
    with pytest.raises(InvalidProductUrlError):
        validate_url_syntax(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/offer/1.html",
        "https://192.168.0.10/x",
        "https://10.0.0.1/x",
        "https://[::1]/x",
    ],
)
def test_rejects_ip_literals(url: str) -> None:
    with pytest.raises(UnsupportedDomainError):
        validate_url_syntax(url)


def test_rejects_localhost() -> None:
    with pytest.raises(UnsupportedDomainError):
        validate_url_syntax("https://localhost/offer/1.html")


def test_rejects_too_long_url() -> None:
    long_url = "https://detail.1688.com/offer/" + ("a" * (MAX_URL_LENGTH + 10)) + ".html"
    with pytest.raises(InvalidProductUrlError):
        validate_url_syntax(long_url)


def test_rejects_empty() -> None:
    with pytest.raises(InvalidProductUrlError):
        validate_url_syntax("")


def test_is_allowed_domain() -> None:
    assert is_allowed_domain("detail.1688.com")
    assert is_allowed_domain("1688.com")
    assert not is_allowed_domain("1688.com.evil.com")
    assert not is_allowed_domain("taobao.com")
