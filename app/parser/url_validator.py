from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

import httpx

from app.exceptions import InvalidProductUrlError, UnsupportedDomainError
from app.logging_config import get_logger

logger = get_logger(__name__)

# Maximum URL length accepted
MAX_URL_LENGTH = 2048

# Allowed 1688 domains (allowlist)
ALLOWED_DOMAINS = {
    "1688.com",
    "detail.1688.com",
    "www.1688.com",
    "m.1688.com",
}

# Allowed URL schemes
ALLOWED_SCHEMES = {"https"}

# Private / reserved IP ranges
_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_ip(hostname: str) -> bool:
    try:
        addr = ipaddress.ip_address(hostname)
        return any(addr in net for net in _PRIVATE_RANGES)
    except ValueError:
        return False


def _domain_is_allowed(hostname: str) -> bool:
    """Return True if hostname is 1688.com or a subdomain of it."""
    hostname = hostname.lower().rstrip(".")
    return hostname == "1688.com" or hostname.endswith(".1688.com")


def _validate_parsed(parsed: "ParseResult") -> None:  # type: ignore[name-defined]
    from urllib.parse import ParseResult  # noqa: F811

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise InvalidProductUrlError(f"Unsupported scheme: {parsed.scheme}")

    hostname = parsed.hostname or ""

    if not hostname:
        raise InvalidProductUrlError("Missing hostname")

    if hostname in ("localhost",) or _is_private_ip(hostname):
        raise InvalidProductUrlError("Private/internal address not allowed")

    if not _domain_is_allowed(hostname):
        raise UnsupportedDomainError(f"Domain not allowed: {hostname}")


class URLValidator:
    """
    Validates that a URL:
    - uses HTTPS
    - points to 1688.com or its subdomains
    - is not a private/localhost address (SSRF protection)
    - resolves after redirects to an allowed domain
    """

    def __init__(self, follow_redirects: bool = True, max_redirects: int = 5) -> None:
        self._follow_redirects = follow_redirects
        self._max_redirects = max_redirects

    async def validate(self, raw_url: str) -> str:
        """
        Validate and normalise the URL. Returns the final URL after redirects.
        Raises InvalidProductUrlError or UnsupportedDomainError on failure.
        """
        url = raw_url.strip()

        if len(url) > MAX_URL_LENGTH:
            raise InvalidProductUrlError("URL too long")

        if not url.startswith("https://") and not url.startswith("http://"):
            raise InvalidProductUrlError("URL must start with https://")

        parsed = urlparse(url)
        _validate_parsed(parsed)  # type: ignore[arg-type]

        if not self._follow_redirects:
            return url

        # Follow redirects safely, re-validating each hop
        final_url = await self._resolve_redirects(url)
        return final_url

    async def _resolve_redirects(self, url: str) -> str:
        """Follow redirects up to max_redirects, re-validating domain at each hop."""
        current_url = url
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=10.0,
        ) as client:
            for _ in range(self._max_redirects):
                try:
                    resp = await client.head(current_url, follow_redirects=False)
                except httpx.RequestError as exc:
                    logger.debug("redirect_follow_failed", url=current_url, error=str(exc))
                    # Cannot follow redirect; just return original validated URL
                    return url

                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "")
                    if not location:
                        break
                    # Handle relative redirect
                    if location.startswith("/"):
                        parsed = urlparse(current_url)
                        location = f"{parsed.scheme}://{parsed.netloc}{location}"
                    parsed_redirect = urlparse(location)
                    try:
                        _validate_parsed(parsed_redirect)  # type: ignore[arg-type]
                    except (InvalidProductUrlError, UnsupportedDomainError):
                        raise UnsupportedDomainError(
                            f"Redirect leads to non-allowed domain: {location}"
                        )
                    current_url = location
                else:
                    break

        return current_url
