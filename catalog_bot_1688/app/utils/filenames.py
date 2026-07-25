"""Safe filename helpers for PDFs and downloaded assets."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_\-]+")
_MULTI_UNDERSCORE = re.compile(r"_+")


def slugify(value: str, *, max_length: int = 40, default: str = "product") -> str:
    """Return an ASCII-safe slug suitable for filenames.

    Non-ASCII characters (e.g. Cyrillic, Chinese) are transliterated where
    possible and otherwise dropped. The result only contains ``[A-Za-z0-9_-]``.
    """
    if not value:
        return default

    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_only = ascii_only.replace(" ", "_")
    cleaned = _SAFE_CHARS.sub("_", ascii_only)
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip("_-")

    if not cleaned:
        return default

    return cleaned[:max_length].strip("_-") or default


def build_catalog_filename(
    brand_name: str,
    product_name: str,
    *,
    when: date | None = None,
) -> str:
    """Build a safe PDF filename: ``Brand_Product_YYYY-MM-DD.pdf``."""
    when = when or date.today()
    brand_slug = slugify(brand_name, max_length=30, default="Catalog")
    product_slug = slugify(product_name, max_length=40, default="product")
    return f"{brand_slug}_{product_slug}_{when.isoformat()}.pdf"


def safe_asset_name(prefix: str, index: int, extension: str = "jpg") -> str:
    """Return a deterministic safe filename for a downloaded asset."""
    prefix_slug = slugify(prefix, max_length=20, default="img")
    extension = extension.lstrip(".").lower() or "jpg"
    return f"{prefix_slug}_{index:03d}.{extension}"
