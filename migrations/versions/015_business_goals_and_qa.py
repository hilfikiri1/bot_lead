"""Business goals and local-first QA feedback.

Revision ID: 015_business_goals_and_qa
Revises: 014_kaizen_journal_entries
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "015_business_goals_and_qa"
down_revision = "014_kaizen_journal_entries"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    created_at, updated_at = _timestamps()
    op.create_table(
        "business_goals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("goal_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="Draft"),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("measurable_result", sa.Text(), nullable=True),
        sa.Column("current_value", sa.Float(), nullable=True),
        sa.Column("target_value", sa.Float(), nullable=True),
        sa.Column("progress_percent", sa.Float(), nullable=True),
        sa.Column("obstacles", sa.Text(), nullable=True),
        sa.Column("next_step", sa.Text(), nullable=True),
        sa.Column("period_outcome", sa.Text(), nullable=True),
        sa.Column(
            "related_project_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "related_task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("notion_page_id", sa.String(length=64), nullable=True),
        sa.Column("notion_url", sa.Text(), nullable=True),
        sa.Column("sync_status", sa.String(length=24), nullable=False, server_default="local"),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        created_at,
        updated_at,
        sa.UniqueConstraint("notion_page_id", name="uq_business_goals_notion_page_id"),
        sa.CheckConstraint(
            "goal_type IN ('Annual', 'Quarterly', 'Monthly', 'Weekly')",
            name="ck_business_goal_type",
        ),
        sa.CheckConstraint(
            "status IN ('Draft', 'Active', 'At risk', 'Blocked', 'Done', 'Cancelled')",
            name="ck_business_goal_status",
        ),
        sa.CheckConstraint(
            "period_end IS NULL OR period_end >= period_start",
            name="ck_business_goal_period_order",
        ),
        sa.CheckConstraint(
            "progress_percent IS NULL OR (progress_percent >= 0 AND progress_percent <= 100)",
            name="ck_business_goal_progress",
        ),
    )
    for column in (
        "telegram_user_id",
        "goal_type",
        "status",
        "period_start",
        "period_end",
        "sync_status",
        "last_checked_at",
    ):
        op.create_index(f"ix_business_goals_{column}", "business_goals", [column])

    created_at, updated_at = _timestamps()
    op.create_table(
        "qa_issues",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("issue_key", sa.String(length=32), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="New"),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="Medium"),
        sa.Column("module", sa.String(length=40), nullable=False, server_default="Other"),
        sa.Column("environment", sa.String(length=20), nullable=False, server_default="production"),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("expected_result", sa.Text(), nullable=True),
        sa.Column("actual_result", sa.Text(), nullable=True),
        sa.Column("reproduction_steps", sa.Text(), nullable=True),
        sa.Column("user_comment", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("related_project_page_id", sa.String(length=64), nullable=True),
        sa.Column("app_version", sa.String(length=64), nullable=True),
        sa.Column("railway_deployment", sa.Text(), nullable=True),
        sa.Column("github_pr_url", sa.Text(), nullable=True),
        sa.Column("similar_issue_key", sa.String(length=32), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("retest_result", sa.String(length=64), nullable=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=True),
        sa.Column("notion_url", sa.Text(), nullable=True),
        sa.Column("sync_status", sa.String(length=24), nullable=False, server_default="local"),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column(
            "diagnostic_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fixed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retested_at", sa.DateTime(timezone=True), nullable=True),
        created_at,
        updated_at,
        sa.UniqueConstraint("issue_key", name="uq_qa_issues_issue_key"),
        sa.UniqueConstraint("notion_page_id", name="uq_qa_issues_notion_page_id"),
        sa.CheckConstraint(
            "issue_type IN ('Bug', 'Improvement', 'UX', 'Concern', 'Question', 'Data issue', 'Integration issue')",
            name="ck_qa_issue_type",
        ),
        sa.CheckConstraint(
            "status IN ('New', 'Need details', 'Confirmed', 'In progress', 'Ready for test', 'Testing', 'Verified', 'Closed', 'Rejected', 'Duplicate', 'Blocked')",
            name="ck_qa_issue_status",
        ),
        sa.CheckConstraint(
            "priority IN ('Critical', 'High', 'Medium', 'Low')",
            name="ck_qa_issue_priority",
        ),
        sa.CheckConstraint(
            "environment IN ('production', 'staging', 'development', 'unknown')",
            name="ck_qa_issue_environment",
        ),
        sa.CheckConstraint(
            "sync_status IN ('local', 'pending', 'synced', 'error')",
            name="ck_qa_issue_sync_status",
        ),
    )
    for column in (
        "issue_key",
        "telegram_user_id",
        "issue_type",
        "status",
        "priority",
        "module",
        "trace_id",
        "kommo_lead_id",
        "sync_status",
        "created_at",
    ):
        op.create_index(f"ix_qa_issues_{column}", "qa_issues", [column])

    created_at, updated_at = _timestamps()
    op.create_table(
        "qa_attachments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "issue_id",
            sa.BigInteger(),
            sa.ForeignKey("qa_issues.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_file_id", sa.Text(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False, server_default="other"),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("drive_file_id", sa.String(length=128), nullable=True),
        sa.Column("drive_url", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        created_at,
        updated_at,
        sa.UniqueConstraint(
            "issue_id", "telegram_file_id", "telegram_message_id",
            name="uq_qa_attachment_telegram_file",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'uploaded', 'failed')",
            name="ck_qa_attachment_status",
        ),
        sa.CheckConstraint(
            "kind IN ('photo', 'screenshot', 'video', 'document', 'log', 'json', 'other')",
            name="ck_qa_attachment_kind",
        ),
    )
    for column in ("issue_id", "status", "checksum_sha256"):
        op.create_index(f"ix_qa_attachments_{column}", "qa_attachments", [column])


def downgrade() -> None:
    op.drop_table("qa_attachments")
    op.drop_table("qa_issues")
    op.drop_table("business_goals")
