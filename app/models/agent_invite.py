from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentInvite(Base):
    """One-time Telegram deep-link invitation. Only the token hash is stored."""

    __tablename__ = "agent_invites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    interface_language: Mapped[str] = mapped_column(
        String(10), nullable=False, default="ru", server_default="ru"
    )
    lead_access_scope: Mapped[str] = mapped_column(
        String(24), nullable=False, default="assigned", server_default="assigned"
    )
    kommo_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending", index=True
    )
    invited_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("agent_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    accepted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
