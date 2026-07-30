from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


class BusinessGoal(Base):
    __tablename__ = "business_goals"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "external_id", name="uq_business_goals_user_external"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(500))
    goal_type: Mapped[str] = mapped_column(String(32), index=True, default="month")
    status: Mapped[str] = mapped_column(String(32), index=True, default="planned")
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    metric_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    target_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    progress_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    obstacles: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_project_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    related_task_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    notion_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class QAIssue(Base):
    __tablename__ = "qa_issues"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "dedupe_key", name="uq_qa_issues_user_dedupe"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    issue_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)
    issue_type: Mapped[str] = mapped_column(String(32), index=True, default="Bug")
    status: Mapped[str] = mapped_column(String(32), index=True, default="New")
    priority: Mapped[str] = mapped_column(String(16), index=True, default="Medium")
    module: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    environment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    reproduction_steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    active_project_number: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kommo_lead_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    app_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    railway_deployment: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_pr: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    retest_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), index=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    notion_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    attachments: Mapped[list["QAAttachment"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", lazy="selectin"
    )


class QAAttachment(Base):
    __tablename__ = "qa_attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("qa_issues.id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    drive_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    drive_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    upload_status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    issue: Mapped[QAIssue] = relationship(back_populates="attachments")
