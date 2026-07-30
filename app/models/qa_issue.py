from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


_JSON = JSON().with_variant(JSONB(), "postgresql")


class QaIssue(Base):
    """Local-first product feedback, bug or operational concern."""

    __tablename__ = "qa_issues"
    __table_args__ = (
        UniqueConstraint("issue_key", name="uq_qa_issues_issue_key"),
        UniqueConstraint("notion_page_id", name="uq_qa_issues_notion_page_id"),
        CheckConstraint(
            "issue_type IN ('Bug', 'Improvement', 'UX', 'Concern', 'Question', "
            "'Data issue', 'Integration issue')",
            name="ck_qa_issue_type",
        ),
        CheckConstraint(
            "status IN ('New', 'Need details', 'Confirmed', 'In progress', "
            "'Ready for test', 'Testing', 'Verified', 'Closed', 'Rejected', "
            "'Duplicate', 'Blocked')",
            name="ck_qa_issue_status",
        ),
        CheckConstraint(
            "priority IN ('Critical', 'High', 'Medium', 'Low')",
            name="ck_qa_issue_priority",
        ),
        CheckConstraint(
            "environment IN ('production', 'staging', 'development', 'unknown')",
            name="ck_qa_issue_environment",
        ),
        CheckConstraint(
            "sync_status IN ('local', 'pending', 'synced', 'error')",
            name="ck_qa_issue_sync_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    issue_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    issue_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="New", server_default="New", index=True
    )
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default="Medium", server_default="Medium", index=True
    )
    module: Mapped[str] = mapped_column(
        String(40), nullable=False, default="Other", server_default="Other", index=True
    )
    environment: Mapped[str] = mapped_column(
        String(20), nullable=False, default="production", server_default="production"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    reproduction_steps: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kommo_lead_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    related_project_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    railway_deployment: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_pr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    similar_issue_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    retest_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="local", server_default="local", index=True
    )
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostic_snapshot: Mapped[dict] = mapped_column(
        _JSON, nullable=False, default=dict, server_default="{}"
    )
    metadata_json: Mapped[dict] = mapped_column(
        _JSON, nullable=False, default=dict, server_default="{}"
    )
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    attachments: Mapped[list["QaAttachment"]] = relationship(
        back_populates="issue", cascade="all, delete-orphan", lazy="selectin"
    )


class QaAttachment(Base):
    """Durable attachment metadata; uploaded is set only after Drive confirms."""

    __tablename__ = "qa_attachments"
    __table_args__ = (
        UniqueConstraint(
            "issue_id", "telegram_file_id", "telegram_message_id",
            name="uq_qa_attachment_telegram_file",
        ),
        CheckConstraint(
            "status IN ('pending', 'uploaded', 'failed')",
            name="ck_qa_attachment_status",
        ),
        CheckConstraint(
            "kind IN ('photo', 'screenshot', 'video', 'document', 'log', 'json', 'other')",
            name="ck_qa_attachment_kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("qa_issues.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    telegram_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="other")
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending", index=True
    )
    drive_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    drive_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    issue: Mapped[QaIssue] = relationship(back_populates="attachments")
