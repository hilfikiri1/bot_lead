from __future__ import annotations

import re
import unicodedata
from datetime import date

SAFE_RE = re.compile(r"[^A-Za-z0-9А-Яа-яЁё_-]+")


def safe_filename(value: str, *, max_length: int = 80) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = SAFE_RE.sub("_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("._-")
    if not normalized:
        normalized = "product"
    return normalized[:max_length].strip("._-") or "product"


def catalog_pdf_filename(product_name: str) -> str:
    short = safe_filename(product_name, max_length=48)
    return f"Babrik_Solutions_{short}_{date.today().isoformat()}.pdf"
