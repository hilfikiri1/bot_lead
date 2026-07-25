"""Safe filename utilities."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

MAX_FILENAME_LENGTH = 80


def safe_filename(name: str, *, max_length: int = MAX_FILENAME_LENGTH) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^\w\-.]", "_", ascii_name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    if not cleaned:
        cleaned = "catalog"
    return cleaned[:max_length]


def build_pdf_filename(product_name: str, created: date | None = None) -> str:
    created = created or date.today()
    short_name = safe_filename(product_name, max_length=40)
    return f"Babrik_Solutions_{short_name}_{created.isoformat()}.pdf"
