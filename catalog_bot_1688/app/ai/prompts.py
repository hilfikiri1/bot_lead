"""Prompt templates for the OpenAI catalog-content step."""

from __future__ import annotations

import json

from app.parser.models import ParsedProduct

SYSTEM_PROMPT = (
    "Ты готовишь содержание коммерческого каталога для B2B-клиента. "
    "Используй только факты, переданные во входных данных. "
    "Переводи китайский текст на русский язык точно и нейтрально. "
    "Не придумывай технические параметры, материалы, сертификаты, комплектацию, "
    "сроки, гарантию или назначение товара. "
    "Не превращай розничную цену в оптовое коммерческое предложение. "
    "Все цены сохраняй в китайских юанях. "
    "Если данные отсутствуют или неоднозначны, используй формулировку "
    "\u201cуточняется у поставщика\u201d либо не добавляй поле. "
    "Описание должно занимать 2\u20134 предложения и не содержать рекламных "
    "преувеличений. "
    "Ответ возвращай только в соответствии с заданной JSON Schema."
)

DEFAULT_DISCLAIMER = (
    "Информация в каталоге сформирована автоматически на основании данных, "
    "размещённых поставщиком на платформе 1688.com. Перевод носит информационный "
    "характер. Цена, минимальный заказ, характеристики, комплектация и наличие "
    "подлежат дополнительному подтверждению у поставщика."
)

PRICE_UNKNOWN_TEXT = "Цена уточняется у поставщика."


def build_user_payload(product: ParsedProduct) -> str:
    """Serialize the parsed product into a compact JSON payload for the model.

    Only text data is included; images are handled locally by the PDF renderer.
    """
    payload = {
        "title_zh": product.title_zh,
        "supplier_name_zh": product.supplier_name_zh,
        "price_raw_text": product.price_raw_text,
        "price_min_cny": _dec(product.price_min_cny),
        "price_max_cny": _dec(product.price_max_cny),
        "price_tiers": [
            {
                "min_quantity": tier.min_quantity,
                "max_quantity": tier.max_quantity,
                "price_cny": _dec(tier.price_cny),
                "raw_text": tier.raw_text,
            }
            for tier in product.price_tiers
        ],
        "moq": product.moq,
        "moq_raw_text": product.moq_raw_text,
        "specifications": [
            {"name_zh": s.name_zh, "value_zh": s.value_zh}
            for s in product.specifications
        ],
        "variants": [
            {"name": v.name, "values": v.values} for v in product.variants
        ],
    }
    instructions = (
        "Ниже данные товара с 1688.com в формате JSON. "
        "Подготовь содержание каталога строго по JSON Schema. "
        "Обязательно заполни поле disclaimer следующим текстом:\n"
        f"{DEFAULT_DISCLAIMER}\n"
        "Если цена отсутствует, в price_display используй: "
        f"\u201c{PRICE_UNKNOWN_TEXT}\u201d.\n\n"
        "ДАННЫЕ ТОВАРА:\n"
    )
    return instructions + json.dumps(payload, ensure_ascii=False, indent=2)


def _dec(value) -> str | None:
    return None if value is None else str(value)
