from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


_JSON = JSON().with_variant(JSONB(), "postgresql")


class KaizenJournalEntry(Base):
    """One local-first daily reflection or weekly kaizen review."""

    __tablename__ = "kaizen_journal_entries"
    __table_args__ = (
        UniqueConstraint(
            "telegram_user_id",
            "entry_type",
            "period_start",
            "period_end",
            name="uq_kaizen_journal_user_type_period",
        ),
        CheckConstraint(
            "entry_type IN ('daily', 'daily_personal', 'weekly')",
            name="ck_kaizen_journal_entry_type",
        ),
        CheckConstraint(
            "status IN ('open', 'completed', 'skipped')",
            name="ck_kaizen_journal_status",
        ),
        CheckConstraint(
            "source IN ('text', 'voice', 'scheduled', 'system')",
            name="ck_kaizen_journal_source",
        ),
        CheckConstraint(
            "period_end >= period_start",
            name="ck_kaizen_journal_period_order",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="open", server_default="open", index=True
    )
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="system", server_default="system"
    )
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    analysis: Mapped[dict] = mapped_column(
        _JSON, nullable=False, default=dict, server_default="{}"
    )
    notion_page_ids: Mapped[list] = mapped_column(
        _JSON, nullable=False, default=list, server_default="[]"
    )
    remind_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
