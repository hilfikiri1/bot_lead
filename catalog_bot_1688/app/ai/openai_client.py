"""OpenAI client wrapper using the Responses API + Structured Outputs.

The model only translates and structures the *text* data. Images are handled by
the local PDF renderer; optionally a single main photo can be attached to help
the model produce a better commercial name, but it is never required.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import ValidationError

from app.ai.prompts import (
    DEFAULT_DISCLAIMER,
    SYSTEM_PROMPT,
    build_user_payload,
)
from app.ai.schemas import CatalogContent, catalog_json_schema
from app.config import Settings
from app.exceptions import OpenAIProcessingError
from app.logging_config import get_logger
from app.parser.models import ParsedProduct
from app.utils.retry import retry_async

logger = get_logger(__name__)

RETRYABLE_ERRORS = (
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    ValidationError,
    json.JSONDecodeError,
)


class OpenAICatalogClient:
    """Generates :class:`CatalogContent` from a :class:`ParsedProduct`."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        self._schema = catalog_json_schema()

    async def generate(
        self, product: ParsedProduct, *, main_image_path: str | None = None
    ) -> CatalogContent:
        """Call OpenAI with retries and return validated catalog content."""
        try:
            return await retry_async(
                lambda: self._generate_once(product, main_image_path),
                attempts=3,
                exceptions=RETRYABLE_ERRORS,
                initial_wait=1.5,
                max_wait=12.0,
            )
        except RETRYABLE_ERRORS as exc:
            logger.error("OpenAI processing failed", error=str(exc))
            raise OpenAIProcessingError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected OpenAI error", error=str(exc))
            raise OpenAIProcessingError(str(exc)) from exc

    async def _generate_once(
        self, product: ParsedProduct, main_image_path: str | None
    ) -> CatalogContent:
        user_content: list[dict] = [
            {"type": "input_text", "text": build_user_payload(product)}
        ]

        image_url = self._encode_image(main_image_path) if main_image_path else None
        if image_url:
            user_content.append({"type": "input_image", "image_url": image_url})

        response = await self._client.responses.create(
            model=self._settings.openai_model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "catalog_content",
                    "schema": self._schema,
                    "strict": True,
                }
            },
        )

        raw = self._extract_output_text(response)
        if not raw:
            raise OpenAIProcessingError("Empty response from OpenAI")

        data = json.loads(raw)
        content = CatalogContent.model_validate(data)

        # Guarantee a disclaimer even if the model omitted it.
        if not content.disclaimer.strip():
            content.disclaimer = DEFAULT_DISCLAIMER
        return content

    @staticmethod
    def _extract_output_text(response) -> str:
        # SDK exposes a convenience aggregate; fall back to manual walk.
        text = getattr(response, "output_text", None)
        if text:
            return text
        chunks: list[str] = []
        for item in getattr(response, "output", []) or []:
            for part in getattr(item, "content", []) or []:
                part_text = getattr(part, "text", None)
                if part_text:
                    chunks.append(part_text)
        return "".join(chunks)

    @staticmethod
    def _encode_image(path: str) -> str | None:
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
