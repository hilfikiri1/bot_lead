from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ClientMessageDraft(Base):
    """Persistent client-facing draft and manual-delivery audit trail."""

    __tablename__ = "client_message_drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    kommo_contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    communication_language: Mapped[str] = mapped_column(String(10), nullable=False)
    language_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recipient: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prepared", server_default="prepared", index=True
    )
    prepared_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("agent_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    last_edited_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_users.id", ondelete="SET NULL"), nullable=True
    )
    sent_confirmed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_users.id", ondelete="SET NULL"), nullable=True
    )
    sent_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_users.id", ondelete="SET NULL"), nullable=True
    )
    delivery_marker: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
