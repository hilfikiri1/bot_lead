"""OpenAI prompts for catalog content generation."""

SYSTEM_PROMPT = """Ты готовишь содержание коммерческого каталога для B2B-клиента.

Используй только факты, переданные во входных данных.
Переводи китайский текст на русский язык точно и нейтрально.
Не придумывай технические параметры, материалы, сертификаты, комплектацию, сроки, гарантию или назначение товара.
Не превращай розничную цену в оптовое коммерческое предложение.
Все цены сохраняй в китайских юанях (CNY, ¥).
Если данные отсутствуют или неоднозначны, используй формулировку «уточняется у поставщика» либо не добавляй поле.
Описание должно занимать 2–4 предложения и не содержать рекламных преувеличений.
Ответ возвращай только в соответствии с заданной JSON Schema.

Запрещено:
- придумывать характеристики;
- определять материал по внешнему виду;
- добавлять CE, ISO или другие сертификаты;
- пересчитывать цену в USD, EUR или PLN;
- добавлять маржу;
- обещать наличие;
- указывать сроки доставки без исходных данных;
- заменять диапазон цен одной выдуманной ценой."""


def build_user_prompt(product_data: dict) -> str:
    lines = [
        "Подготовь содержание каталога на основе следующих данных:",
        f"Китайское название: {product_data.get('title_zh', '')}",
    ]

    if product_data.get("price_raw_text"):
        lines.append(f"Цена (исходный текст): {product_data['price_raw_text']}")
    if product_data.get("price_min_cny") is not None:
        lines.append(f"Минимальная цена CNY: {product_data['price_min_cny']}")
    if product_data.get("price_max_cny") is not None:
        lines.append(f"Максимальная цена CNY: {product_data['price_max_cny']}")

    if product_data.get("moq_raw_text"):
        lines.append(f"MOQ: {product_data['moq_raw_text']}")
    elif product_data.get("moq"):
        lines.append(f"MOQ: {product_data['moq']}")

    if product_data.get("supplier_name_zh"):
        lines.append(f"Поставщик: {product_data['supplier_name_zh']}")

    if product_data.get("price_tiers"):
        lines.append("Ступенчатые цены:")
        for tier in product_data["price_tiers"]:
            lines.append(f"  - {tier}")

    if product_data.get("specifications"):
        lines.append("Характеристики:")
        for spec in product_data["specifications"]:
            lines.append(f"  - {spec.get('name_zh', '')}: {spec.get('value_zh', '')}")

    if product_data.get("variants"):
        lines.append("Варианты:")
        for var in product_data["variants"]:
            lines.append(f"  - {var.get('name', '')}: {', '.join(var.get('values', []))}")

    if not product_data.get("price_raw_text") and product_data.get("price_min_cny") is None:
        lines.append("Цена отсутствует — используй «Цена уточняется у поставщика».")

    return "\n".join(lines)
