from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from openai import AsyncOpenAI, RateLimitError, APIStatusError, APITimeoutError

from app.ai.prompts import CATALOG_DISCLAIMER, SYSTEM_PROMPT, build_user_message
from app.ai.schemas import CATALOG_CONTENT_SCHEMA, CatalogContent, CatalogPriceTier, CatalogSpecification
from app.config import settings
from app.exceptions import OpenAIProcessingError
from app.logging_config import get_logger
from app.parser.models import ParsedProduct

logger = get_logger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF = 2.0


class OpenAIClient:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    async def generate_catalog_content(
        self,
        product: ParsedProduct,
        main_image_path: Optional[Path] = None,
    ) -> CatalogContent:
        """
        Send product data to OpenAI and receive structured catalog content.
        Uses Structured Outputs with strict JSON Schema.
        """
        user_message = build_user_message(
            title_zh=product.title_zh,
            price_raw=product.price_raw_text,
            price_tiers_raw=[
                t.raw_text for t in product.price_tiers if t.raw_text
            ],
            moq_raw=product.moq_raw_text,
            specifications=[
                (s.name_zh, s.value_zh) for s in product.specifications
            ],
            variants=[
                (v.name, v.values) for v in product.variants
            ],
            supplier=product.supplier_name_zh,
        )

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=settings.openai_model,
                    messages=messages,  # type: ignore[arg-type]
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "catalog_content",
                            "strict": True,
                            "schema": CATALOG_CONTENT_SCHEMA,
                        },
                    },
                    temperature=0.1,
                    max_tokens=2048,
                )

                raw_json = response.choices[0].message.content
                if not raw_json:
                    raise OpenAIProcessingError("Empty response from OpenAI")

                data = json.loads(raw_json)
                content = _build_catalog_content(data)
                logger.info(
                    "openai_content_generated",
                    product_name=content.product_name_ru[:60],
                    attempt=attempt,
                )
                return content

            except (RateLimitError, APITimeoutError) as exc:
                last_exc = exc
                wait = INITIAL_BACKOFF * (2 ** (attempt - 1))
                logger.warning(
                    "openai_retry",
                    attempt=attempt,
                    error=type(exc).__name__,
                    wait=wait,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(wait)

            except APIStatusError as exc:
                if exc.status_code >= 500:
                    last_exc = exc
                    wait = INITIAL_BACKOFF * (2 ** (attempt - 1))
                    logger.warning(
                        "openai_server_error_retry",
                        attempt=attempt,
                        status=exc.status_code,
                        wait=wait,
                    )
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(wait)
                else:
                    raise OpenAIProcessingError(f"OpenAI API error: {exc}") from exc

            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "openai_invalid_response",
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(INITIAL_BACKOFF)

        raise OpenAIProcessingError(
            f"OpenAI failed after {MAX_RETRIES} attempts: {last_exc}"
        )


def _build_catalog_content(data: dict) -> CatalogContent:
    """Build CatalogContent from raw OpenAI JSON dict."""
    price_tiers = [
        CatalogPriceTier(
            quantity=t.get("quantity", ""),
            price=t.get("price", ""),
        )
        for t in data.get("price_tiers", [])
    ]
    specifications = [
        CatalogSpecification(
            name=s.get("name", ""),
            value=s.get("value", ""),
        )
        for s in data.get("specifications", [])
    ]
    variants = [
        CatalogSpecification(
            name=v.get("name", ""),
            value=v.get("value", ""),
        )
        for v in data.get("variants", [])
    ]

    disclaimer = data.get("disclaimer") or CATALOG_DISCLAIMER

    return CatalogContent(
        product_name_ru=data["product_name_ru"],
        original_name_zh=data["original_name_zh"],
        short_description_ru=data["short_description_ru"],
        supplier_name=data.get("supplier_name"),
        price_display=data["price_display"],
        price_note=data.get("price_note"),
        moq_display=data.get("moq_display"),
        price_tiers=price_tiers,
        specifications=specifications,
        variants=variants,
        disclaimer=disclaimer,
    )
