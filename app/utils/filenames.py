from __future__ import annotations

import re
from datetime import datetime, timezone


def safe_pdf_filename(product_name_ru: str, max_name_len: int = 40) -> str:
    """
    Generate a safe PDF filename:
      Babrik_Solutions_<short_product_name>_<YYYY-MM-DD>.pdf
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Keep only alphanumeric, spaces, hyphens, underscores
    safe_name = re.sub(r"[^\w\s\-]", "", product_name_ru, flags=re.UNICODE)
    # Replace whitespace sequences with single underscore
    safe_name = re.sub(r"\s+", "_", safe_name.strip())
    # Truncate
    safe_name = safe_name[:max_name_len]
    return f"Babrik_Solutions_{safe_name}_{today}.pdf"
