from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentUser(Base):
    """A Telegram user allowed to work with the B&BS agent."""

    __tablename__ = "agent_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True
    )
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(
        String(24), nullable=False, default="manager", server_default="manager", index=True
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", server_default="active", index=True
    )
    interface_language: Mapped[str] = mapped_column(
        String(10), nullable=False, default="ru", server_default="ru"
    )
    lead_access_scope: Mapped[str] = mapped_column(
        String(24), nullable=False, default="assigned", server_default="assigned"
    )
    kommo_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    invited_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_users.id", ondelete="SET NULL"), nullable=True
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
