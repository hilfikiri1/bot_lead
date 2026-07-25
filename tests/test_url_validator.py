import pytest

from app.parser.errors import InvalidProductUrlError, UnsupportedDomainError
from app.parser import url_validator
from app.parser.url_validator import validate_product_url


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def ok(hostname: str) -> bool:
        return hostname not in {"127.0.0.1", "localhost"}
    monkeypatch.setattr(url_validator, "_resolved_addresses_are_public", ok)


@pytest.mark.asyncio
async def test_allows_detail_1688_url():
    result = await validate_product_url("https://detail.1688.com/offer/123.html")
    assert str(result.final_url) == "https://detail.1688.com/offer/123.html"


@pytest.mark.asyncio
async def test_blocks_other_domain_without_redirect():
    with pytest.raises(UnsupportedDomainError):
        await validate_product_url("https://example.com/product", follow_redirects=False)


@pytest.mark.asyncio
async def test_blocks_internal_ip():
    with pytest.raises(InvalidProductUrlError):
        await validate_product_url("https://127.0.0.1/offer/1.html")


@pytest.mark.asyncio
async def test_blocks_non_https():
    with pytest.raises(InvalidProductUrlError):
        await validate_product_url("http://detail.1688.com/offer/123.html")
