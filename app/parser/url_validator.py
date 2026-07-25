from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse, urlunparse

import httpx

from app.parser.errors import InvalidProductUrlError, UnsupportedDomainError
from app.parser.models import ValidatedProductUrl

ALLOWED_DOMAIN = "1688.com"
MAX_URL_LENGTH = 2048
ALLOWED_SCHEME = "https"
PRODUCT_PATH_HINTS = ("/offer/", "/detail/", "/shop/offer/", "detail.")


def _is_allowed_1688_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    return normalized == ALLOWED_DOMAIN or normalized.endswith(f".{ALLOWED_DOMAIN}")


def _is_private_host_literal(hostname: str | None) -> bool:
    if not hostname:
        return True
    lowered = hostname.lower().strip("[]")
    if lowered in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


async def _resolved_addresses_are_public(hostname: str) -> bool:
    if _is_private_host_literal(hostname):
        return False

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for _, _, _, _, sockaddr in infos:
        address = sockaddr[0]
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _normalize_url(raw_url: str) -> str:
    raw = raw_url.strip()
    if len(raw) > MAX_URL_LENGTH:
        raise InvalidProductUrlError("URL is too long")
    parsed = urlparse(raw)
    if parsed.scheme != ALLOWED_SCHEME or not parsed.netloc:
        raise InvalidProductUrlError("Only HTTPS URLs are accepted")
    if parsed.username or parsed.password:
        raise InvalidProductUrlError("Credentials in URL are not accepted")
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.params, parsed.query, ""))


def looks_like_product_url(url: str) -> bool:
    parsed = urlparse(url)
    if not _is_allowed_1688_host(parsed.hostname):
        return False
    path = parsed.path.lower()
    host = (parsed.hostname or "").lower()
    return host.startswith("detail.") or any(hint in path for hint in PRODUCT_PATH_HINTS)


async def validate_product_url(raw_url: str, *, follow_redirects: bool = True) -> ValidatedProductUrl:
    normalized = _normalize_url(raw_url)
    parsed = urlparse(normalized)
    if not await _resolved_addresses_are_public(parsed.hostname or ""):
        raise InvalidProductUrlError("URL resolves to a blocked network")

    if _is_allowed_1688_host(parsed.hostname):
        if not looks_like_product_url(normalized):
            raise InvalidProductUrlError("URL is not a recognizable 1688 product page")
        return ValidatedProductUrl(original_url=raw_url, final_url=normalized)

    if not follow_redirects:
        raise UnsupportedDomainError("Only 1688.com URLs are accepted")

    current = normalized
    async with httpx.AsyncClient(follow_redirects=False, timeout=8.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for _ in range(5):
            response = await client.get(current)
            location = response.headers.get("location")
            if response.status_code not in {301, 302, 303, 307, 308} or not location:
                break
            next_url = str(httpx.URL(current).join(location))
            next_url = _normalize_url(next_url)
            parsed_next = urlparse(next_url)
            if not await _resolved_addresses_are_public(parsed_next.hostname or ""):
                raise InvalidProductUrlError("Redirect resolves to a blocked network")
            current = next_url

    final_parsed = urlparse(current)
    if not _is_allowed_1688_host(final_parsed.hostname) or not looks_like_product_url(current):
        raise UnsupportedDomainError("Redirect target is not a 1688 product page")
    return ValidatedProductUrl(original_url=raw_url, final_url=current)
