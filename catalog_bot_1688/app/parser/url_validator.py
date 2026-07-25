"""SSRF-safe URL validation for 1688 product links.

The validator enforces:
* HTTPS only;
* a strict allowlist of ``1688.com`` (and sub-domains);
* rejection of internal/loopback/private/reserved IP addresses;
* rejection of non-HTTP protocols (``file://``, ``ftp://`` ...);
* a maximum URL length;
* re-validation of the final host after any redirect chain.

Short links (e.g. ``qr.1688.com`` / ``m.1688.com`` share links) are resolved via
HEAD/GET requests that follow redirects manually while re-checking the domain at
every hop.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from app.exceptions import InvalidProductUrlError, UnsupportedDomainError

MAX_URL_LENGTH = 2048
ALLOWED_SCHEME = "https"

# Allowlist: the apex domain and any sub-domain of 1688.com.
ALLOWED_BASE_DOMAINS = ("1688.com",)

# Sub-domains that are known to host actual product detail pages. Short-link
# hosts (qr / s / m) are allowed as *entry* points but must redirect into one of
# these before we accept them.
PRODUCT_HOST_HINTS = ("detail.1688.com", "m.1688.com")

MAX_REDIRECTS = 5


def _normalize_host(host: str | None) -> str:
    if not host:
        return ""
    return host.strip().lower().rstrip(".")


def is_allowed_domain(host: str) -> bool:
    """Return True if ``host`` is 1688.com or one of its sub-domains."""
    host = _normalize_host(host)
    if not host:
        return False
    return any(
        host == base or host.endswith("." + base) for base in ALLOWED_BASE_DOMAINS
    )


def _is_public_ip(host: str) -> bool:
    """Return True only if ``host`` resolves exclusively to public IP addresses."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Cannot resolve — treat as unsafe.
        return False

    resolved = {info[4][0] for info in infos}
    if not resolved:
        return False

    for address in resolved:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def validate_url_syntax(url: str) -> str:
    """Validate the static shape of a URL (scheme, length, host allowlist).

    Raises :class:`InvalidProductUrlError` or :class:`UnsupportedDomainError`.
    Returns the cleaned URL string on success.
    """
    if not url or not isinstance(url, str):
        raise InvalidProductUrlError()

    url = url.strip()

    if len(url) > MAX_URL_LENGTH:
        raise InvalidProductUrlError("URL too long")

    parsed = urlparse(url)

    if parsed.scheme.lower() != ALLOWED_SCHEME:
        # Blocks http, file, ftp, gopher, data, etc.
        raise InvalidProductUrlError("Only HTTPS URLs are supported")

    host = _normalize_host(parsed.hostname)
    if not host:
        raise InvalidProductUrlError("URL has no host")

    # Reject raw IP literals outright — product links always use a hostname.
    try:
        ipaddress.ip_address(host)
        raise UnsupportedDomainError("IP literals are not allowed")
    except ValueError:
        pass

    if not is_allowed_domain(host):
        raise UnsupportedDomainError()

    return url


def _check_hop(url: str) -> str:
    """Validate a single redirect hop (scheme + domain + public IP)."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != ALLOWED_SCHEME:
        raise UnsupportedDomainError("Redirect left HTTPS")
    host = _normalize_host(parsed.hostname)
    if not is_allowed_domain(host):
        raise UnsupportedDomainError("Redirect left the 1688.com allowlist")
    if not _is_public_ip(host):
        raise UnsupportedDomainError("Host resolves to a non-public address")
    return url


async def resolve_and_validate(url: str, *, client: httpx.AsyncClient | None = None) -> str:
    """Validate syntax then follow redirects manually, re-checking every hop.

    Returns the final validated URL. Raises on any policy violation.
    """
    url = validate_url_syntax(url)
    _check_hop(url)

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BabrikCatalogBot/1.0)"},
        )

    current = url
    try:
        for _ in range(MAX_REDIRECTS):
            try:
                response = await client.head(current)
            except httpx.HTTPError:
                # Some 1688 hosts reject HEAD — fall back to a lightweight GET.
                try:
                    response = await client.get(current)
                except httpx.HTTPError as exc:
                    raise InvalidProductUrlError("Could not reach the URL") from exc

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    break
                current = str(httpx.URL(current).join(location))
                _check_hop(current)
                continue
            break
        else:
            raise InvalidProductUrlError("Too many redirects")
    finally:
        if owns_client:
            await client.aclose()

    return _check_hop(current)
