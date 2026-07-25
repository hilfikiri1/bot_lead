from __future__ import annotations

import json
from typing import Any

import structlog
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, RateLimitError
from pydantic import ValidationError

from app.ai.prompts import SYSTEM_PROMPT, build_user_prompt
from app.ai.schemas import CatalogContent
from app.config import Settings
from app.parser.errors import OpenAIProcessingError
from app.parser.models import ParsedProduct
from app.utils.retry import async_retry

logger = structlog.get_logger(__name__)


class OpenAICatalogClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    async def build_catalog_content(self, product: ParsedProduct) -> CatalogContent:
        if self.client is None:
            raise OpenAIProcessingError("OPENAI_API_KEY is not configured")
        return await async_retry(lambda: self._call(product), attempts=3, retry_exceptions=(RateLimitError, APITimeoutError, APIConnectionError, OpenAIProcessingError))

    async def _call(self, product: ParsedProduct) -> CatalogContent:
        payload = self._payload(product)
        try:
            response = await self.client.responses.create(
                model=self.settings.openai_model,
                input=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": build_user_prompt(payload)}],
                text={"format": {"type": "json_schema", "name": "catalog_content", "schema": CatalogContent.model_json_schema(), "strict": True}},
            )
            return CatalogContent.model_validate_json(response.output_text)
        except ValidationError as exc:
            logger.warning("openai_structured_output_invalid", error=str(exc))
            raise OpenAIProcessingError("Invalid structured output") from exc
        except Exception as exc:
            if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError, OpenAIProcessingError)):
                raise
            raise OpenAIProcessingError(str(exc)) from exc

    def _payload(self, product: ParsedProduct) -> dict[str, Any]:
        return json.loads(product.model_dump_json())
