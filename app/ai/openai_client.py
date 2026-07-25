"""OpenAI Responses API client with Structured Outputs."""

from __future__ import annotations

import json
from typing import Any

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.ai.prompts import SYSTEM_PROMPT, build_user_prompt
from app.ai.schemas import CatalogContent, catalog_content_json_schema
from app.config import Settings, get_settings
from app.exceptions import OpenAIProcessingError
from app.logging_config import get_logger
from app.parser.models import ParsedProduct
from app.utils.retry import async_retry

logger = get_logger(__name__)

RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, OpenAIProcessingError)


class OpenAICatalogClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = AsyncOpenAI(api_key=self.settings.openai_api_key)

    @async_retry(max_attempts=3, delay=2.0, retryable=RETRYABLE)
    async def generate_catalog_content(self, product: ParsedProduct) -> CatalogContent:
        product_data = self._product_to_dict(product)
        user_prompt = build_user_prompt(product_data)

        try:
            response = await self.client.responses.create(
                model=self.settings.openai_model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "catalog_content",
                        "schema": catalog_content_json_schema(),
                        "strict": True,
                    }
                },
            )
        except Exception as exc:
            logger.exception("openai_request_failed", error=str(exc))
            raise OpenAIProcessingError(str(exc)) from exc

        raw_text = self._extract_output_text(response)
        if not raw_text:
            raise OpenAIProcessingError("Empty response from OpenAI")

        try:
            data = json.loads(raw_text)
            content = CatalogContent.model_validate(data)
        except Exception as exc:
            logger.error("openai_invalid_schema", raw=raw_text[:500])
            raise OpenAIProcessingError(f"Invalid structured response: {exc}") from exc

        if not content.price_display or content.price_display.strip() == "":
            content.price_display = "Цена уточняется у поставщика."

        return content

    def _product_to_dict(self, product: ParsedProduct) -> dict[str, Any]:
        return {
            "title_zh": product.title_zh,
            "supplier_name_zh": product.supplier_name_zh,
            "price_raw_text": product.price_raw_text,
            "price_min_cny": str(product.price_min_cny) if product.price_min_cny else None,
            "price_max_cny": str(product.price_max_cny) if product.price_max_cny else None,
            "moq": product.moq,
            "moq_raw_text": product.moq_raw_text,
            "price_tiers": [
                {
                    "min": t.min_quantity,
                    "max": t.max_quantity,
                    "price": str(t.price_cny) if t.price_cny else t.raw_text,
                }
                for t in product.price_tiers
            ],
            "specifications": [
                {"name_zh": s.name_zh, "value_zh": s.value_zh} for s in product.specifications
            ],
            "variants": [{"name": v.name, "values": v.values} for v in product.variants],
        }

    def _extract_output_text(self, response: Any) -> str | None:
        if hasattr(response, "output_text") and response.output_text:
            return response.output_text

        output = getattr(response, "output", None)
        if not output:
            return None

        parts: list[str] = []
        for item in output:
            content = getattr(item, "content", None)
            if not content:
                continue
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
        return "".join(parts) if parts else None
