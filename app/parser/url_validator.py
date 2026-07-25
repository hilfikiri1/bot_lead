"""URL validation with SSRF protection for 1688 product links."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

import httpx

from app.exceptions import InvalidProductUrlError, UnsupportedDomainError

MAX_URL_LENGTH = 2048
ALLOWED_DOMAIN_SUFFIX = "1688.com"
PRODUCT_PATH_PATTERNS = (
    re.compile(r"/offer/\d+", re.I),
    re.compile(r"/detail\.1688\.com/", re.I),
    re.compile(r"detail\.1688\.com", re.I),
    re.compile(r"/product/\d+", re.I),
)

BLOCKED_SCHEMES = {"file", "ftp", "data", "javascript", "mailto", "tel"}


def _is_private_ip(hostname: str) -> bool:
    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        return False


def _is_allowed_domain(hostname: str) -> bool:
    hostname = hostname.lower().rstrip(".")
    return hostname == ALLOWED_DOMAIN_SUFFIX or hostname.endswith(f".{ALLOWED_DOMAIN_SUFFIX}")


def _is_product_path(path: str, hostname: str) -> bool:
    if "detail.1688.com" in hostname.lower():
        return True
    return any(pattern.search(path) for pattern in PRODUCT_PATH_PATTERNS)


def validate_url_format(url: str) -> str:
    """Validate URL format and domain without following redirects."""
    if not url or not isinstance(url, str):
        raise InvalidProductUrlError()

    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise InvalidProductUrlError()

    parsed = urlparse(url)

    if parsed.scheme.lower() not in {"https"}:
        raise UnsupportedDomainError()

    if parsed.scheme.lower() in BLOCKED_SCHEMES:
        raise UnsupportedDomainError()

    hostname = parsed.hostname
    if not hostname:
        raise InvalidProductUrlError()

    if _is_private_ip(hostname):
        raise UnsupportedDomainError()

    if hostname.lower() in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        raise UnsupportedDomainError()

    if not _is_allowed_domain(hostname):
        raise UnsupportedDomainError()

    if not _is_product_path(parsed.path or "", hostname):
        raise InvalidProductUrlError()

    return url


async def resolve_and_validate_url(url: str, *, timeout: float = 15.0) -> str:
    """Follow redirects safely and re-validate domain at each hop."""
    url = validate_url_format(url)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BabrikCatalogBot/1.0)"},
    ) as client:
        try:
            response = await client.head(url)
            final_url = str(response.url)
        except httpx.HTTPError:
            try:
                response = await client.get(url)
                final_url = str(response.url)
            except httpx.HTTPError as exc:
                raise InvalidProductUrlError(str(exc)) from exc

    return validate_url_format(final_url)


def normalize_1688_url(url: str) -> str:
    """Normalize URL without network requests."""
    return validate_url_format(url.strip())
