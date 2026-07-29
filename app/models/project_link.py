from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ProjectLink(Base):
    """Unified link between Kommo lead, Notion project and Google Drive folder."""

    __tablename__ = "project_links"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    internal_lead_number: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    kommo_lead_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notion_project_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_project_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_folder_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    drive_folder_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_folder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="active")
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
