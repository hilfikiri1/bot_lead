from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProjectArtifact(Base):
    """Audited project file from Telegram preview through external sync."""

    __tablename__ = "project_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id",
            "telegram_message_id",
            name="uq_project_artifacts_telegram_message",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_link_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("project_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    suggested_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    final_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    artifact_type_label: Mapped[str] = mapped_column(String(128), nullable=False)
    classification_source: Mapped[str] = mapped_column(String(32), nullable=False)
    subfolder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending", index=True
    )
    drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    drive_file_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    kommo_note_created: Mapped[bool] = mapped_column(nullable=False, default=False)
    warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    uploaded_by_telegram_user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
