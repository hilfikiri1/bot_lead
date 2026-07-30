from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
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


class BusinessGoal(Base):
    """Local-first cache and audit record for one measurable business goal."""

    __tablename__ = "business_goals"
    __table_args__ = (
        UniqueConstraint("notion_page_id", name="uq_business_goals_notion_page_id"),
        CheckConstraint(
            "goal_type IN ('Annual', 'Quarterly', 'Monthly', 'Weekly')",
            name="ck_business_goal_type",
        ),
        CheckConstraint(
            "status IN ('Draft', 'Active', 'At risk', 'Blocked', 'Done', 'Cancelled')",
            name="ck_business_goal_status",
        ),
        CheckConstraint(
            "period_end IS NULL OR period_end >= period_start",
            name="ck_business_goal_period_order",
        ),
        CheckConstraint(
            "progress_percent IS NULL OR (progress_percent >= 0 AND progress_percent <= 100)",
            name="ck_business_goal_progress",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    goal_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="Draft", server_default="Draft", index=True
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    measurable_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    progress_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    obstacles: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_project_ids: Mapped[list] = mapped_column(
        _JSON, nullable=False, default=list, server_default="[]"
    )
    related_task_ids: Mapped[list] = mapped_column(
        _JSON, nullable=False, default=list, server_default="[]"
    )
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="local", server_default="local", index=True
    )
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    metadata_json: Mapped[dict] = mapped_column(
        _JSON, nullable=False, default=dict, server_default="{}"
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
