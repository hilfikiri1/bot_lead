"""ORM models. Only non-sensitive task metadata is stored.

Secrets (Telegram token, OpenAI key, cookies) are NEVER persisted here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class JobStatus(StrEnum):
    RECEIVED = "received"
    VALIDATING = "validating"
    PARSING = "parsing"
    DOWNLOADING_IMAGES = "downloading_images"
    GENERATING_CONTENT = "generating_content"
    RENDERING_PDF = "rendering_pdf"
    COMPLETED = "completed"
    FAILED = "failed"


ACTIVE_STATUSES = (
    JobStatus.RECEIVED,
    JobStatus.VALIDATING,
    JobStatus.PARSING,
    JobStatus.DOWNLOADING_IMAGES,
    JobStatus.GENERATING_CONTENT,
    JobStatus.RENDERING_PDF,
)


class CatalogJob(Base):
    __tablename__ = "catalog_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.RECEIVED.value, index=True
    )
    product_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
