import pytest

from app.exceptions import InvalidProductUrlError, UnsupportedDomainError
from app.parser.url_validator import validate_1688_url


def test_accepts_valid_1688_url() -> None:
    url = "https://detail.1688.com/offer/123456.html"
    assert validate_1688_url(url) == url


def test_blocks_non_1688_domain() -> None:
    with pytest.raises(UnsupportedDomainError):
        validate_1688_url("https://example.com/item/1")


def test_blocks_localhost() -> None:
    with pytest.raises(UnsupportedDomainError):
        validate_1688_url("https://localhost/item")


def test_blocks_private_ip() -> None:
    with pytest.raises(UnsupportedDomainError):
        validate_1688_url("https://192.168.0.10/offer")


def test_blocks_non_https_protocol() -> None:
    with pytest.raises(InvalidProductUrlError):
        validate_1688_url("ftp://detail.1688.com/offer/1")
