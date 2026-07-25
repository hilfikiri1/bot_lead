"""API schemas for Chrome extension batch catalog requests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


class BatchProductInput(BaseModel):
    source_url: str
    title_zh: str
    supplier_name_zh: str | None = None
    price_raw_text: str | None = None
    price_min_cny: Decimal | None = None
    price_max_cny: Decimal | None = None
    thumbnail_url: str | None = None
    moq_raw_text: str | None = None
    offer_id: str | None = None

    @field_validator("source_url", "title_zh")
    @classmethod
    def strip_required(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field cannot be empty")
        return cleaned

    @field_validator("thumbnail_url", "supplier_name_zh", "price_raw_text", "moq_raw_text", mode="before")
    @classmethod
    def empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class BatchCatalogOptions(BaseModel):
    locale: str = "ru"
    telegram_user_id: int | None = None
    telegram_chat_id: int | None = None
    source_page_url: str | None = None


class BatchCatalogRequest(BaseModel):
    products: list[BatchProductInput] = Field(min_length=1)
    options: BatchCatalogOptions = Field(default_factory=BatchCatalogOptions)


class BatchCatalogResponse(BaseModel):
    job_id: str
    status: str
    product_count: int


class BatchJobStatusResponse(BaseModel):
    job_id: str
    status: str
    product_count: int | None = None
    product_title: str | None = None
    output_file_url: str | None = None
    error_message: str | None = None
    progress: int | None = None
