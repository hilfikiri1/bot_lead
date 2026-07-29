from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentSession(Base):
    """Persistent conversational memory for one Telegram manager."""

    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True
    )
    active_kommo_lead_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    active_local_lead_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )
    memory_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_intent: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_user_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_assistant_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
