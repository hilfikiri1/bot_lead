from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlparse

import httpx

from app.exceptions import InvalidProductUrlError, UnsupportedDomainError

ALLOWED_SCHEMES = {"https"}
ALLOWED_ROOT_DOMAIN = "1688.com"
MAX_URL_LENGTH = 2048


def _is_allowed_domain(hostname: str) -> bool:
    return hostname == ALLOWED_ROOT_DOMAIN or hostname.endswith(f".{ALLOWED_ROOT_DOMAIN}")


def _is_private_or_local_host(hostname: str) -> bool:
    lowered = hostname.lower()
    if lowered in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def validate_1688_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise InvalidProductUrlError("URL is empty or too long")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidProductUrlError("Only HTTPS links are allowed")
    if not parsed.hostname:
        raise InvalidProductUrlError("Hostname is missing")
    if _is_private_or_local_host(parsed.hostname):
        raise UnsupportedDomainError("Private or local hosts are blocked")
    if not _is_allowed_domain(parsed.hostname):
        raise UnsupportedDomainError("Only 1688 domain is allowed")
    return url


async def resolve_and_validate_redirects(raw_url: str, max_redirects: int = 5) -> str:
    validated = validate_1688_url(raw_url)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            current = validated
            for _ in range(max_redirects + 1):
                response = await client.get(current, headers={"User-Agent": "Mozilla/5.0"})
                if response.status_code in {301, 302, 303, 307, 308}:
                    next_location = response.headers.get("Location")
                    if not next_location:
                        raise InvalidProductUrlError("Redirect without Location header")
                    current = urljoin(current, next_location)
                    validate_1688_url(current)
                    continue
                validate_1688_url(current)
                return current
    except httpx.HTTPError:
        return validated
    raise InvalidProductUrlError("Too many redirects")
