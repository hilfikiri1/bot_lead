from __future__ import annotations

import re
from datetime import date


def sanitize_filename_part(text: str, max_length: int = 48) -> str:
    normalized = re.sub(r"[^A-Za-z0-9А-Яа-яЁё一-龥]+", "_", text.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        normalized = "product"
    return normalized[:max_length]


def build_pdf_filename(product_name: str) -> str:
    short = sanitize_filename_part(product_name, max_length=32)
    return f"Babrik_Solutions_{short}_{date.today().isoformat()}.pdf"
