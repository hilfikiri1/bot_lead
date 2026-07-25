from __future__ import annotations

import json
from collections.abc import Sequence

from openai import APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.ai.prompts import SYSTEM_PROMPT
from app.ai.schemas import CATALOG_CONTENT_SCHEMA, CatalogContent
from app.config import get_settings
from app.exceptions import OpenAIProcessingError
from app.parser.models import ParsedProduct
from app.utils.retry import retry_async


def _build_user_payload(product: ParsedProduct) -> dict:
    return {
        "title_zh": product.title_zh,
        "supplier_name_zh": product.supplier_name_zh,
        "price_raw_text": product.price_raw_text,
        "price_min_cny": str(product.price_min_cny) if product.price_min_cny is not None else None,
        "price_max_cny": str(product.price_max_cny) if product.price_max_cny is not None else None,
        "price_tiers": [tier.model_dump() for tier in product.price_tiers],
        "moq": product.moq,
        "moq_raw_text": product.moq_raw_text,
        "variants": [variant.model_dump() for variant in product.variants],
        "specifications": [spec.model_dump() for spec in product.specifications],
        "source_url": str(product.source_url),
    }


def _extract_text_output(response) -> str:
    if getattr(response, "output_text", None):
        return response.output_text
    chunks: list[str] = []
    output: Sequence = getattr(response, "output", [])
    for item in output:
        content = getattr(item, "content", None) or []
        for part in content:
            text = getattr(part, "text", None)
            if text:
                chunks.append(text)
    if not chunks:
        raise OpenAIProcessingError("OpenAI returned empty output")
    return "\n".join(chunks)


class OpenAIContentClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_model

    @retry_async(
        max_attempts=3,
        retry_on=(RateLimitError, APITimeoutError, APIError, OpenAIProcessingError),
    )
    async def generate_catalog_content(self, product: ParsedProduct) -> CatalogContent:
        payload = _build_user_payload(product)
        response = await self._client.responses.create(
            model=self._model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": CATALOG_CONTENT_SCHEMA["name"],
                    "schema": CATALOG_CONTENT_SCHEMA["schema"],
                    "strict": True,
                }
            },
        )
        try:
            text_output = _extract_text_output(response)
            parsed = CatalogContent.model_validate(json.loads(text_output))
            return parsed
        except Exception as exc:
            raise OpenAIProcessingError("Invalid structured output") from exc
