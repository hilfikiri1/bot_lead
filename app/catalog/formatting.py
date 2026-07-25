from __future__ import annotations

from decimal import Decimal

from app.parser.models import ParsedProduct


def _fmt_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    as_text = format(normalized, "f")
    return as_text.rstrip("0").rstrip(".") if "." in as_text else as_text


def format_price_display(product: ParsedProduct) -> str:
    if product.price_min_cny is None and product.price_max_cny is None and product.price_raw_text:
        return product.price_raw_text
    if product.price_min_cny is None and product.price_max_cny is None:
        return "Цена уточняется у поставщика."
    if product.price_min_cny is not None and product.price_max_cny is not None:
        if product.price_min_cny == product.price_max_cny:
            return f"{_fmt_decimal(product.price_min_cny)} CNY"
        return f"{_fmt_decimal(product.price_min_cny)}–{_fmt_decimal(product.price_max_cny)} CNY"
    value = product.price_min_cny or product.price_max_cny
    return f"{_fmt_decimal(value)} CNY"
