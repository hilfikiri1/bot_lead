"""Goals and QA operating system.

Revision ID: 015_goals_and_qa
Revises: 014_kaizen_journal_entries
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "015_goals_and_qa"
down_revision = "014_kaizen_journal_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_goals",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("goal_type", sa.String(length=32), nullable=False, server_default="month"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="planned"),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("metric_name", sa.String(length=255), nullable=True),
        sa.Column("current_value", sa.Numeric(18, 4), nullable=True),
        sa.Column("target_value", sa.Numeric(18, 4), nullable=True),
        sa.Column("progress_percent", sa.Numeric(6, 2), nullable=True),
        sa.Column("obstacles", sa.Text(), nullable=True),
        sa.Column("next_step", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("related_project_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("related_task_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notion_page_id", sa.String(length=128), nullable=True),
        sa.Column("notion_url", sa.Text(), nullable=True),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("telegram_user_id", "external_id", name="uq_business_goals_user_external"),
        sa.CheckConstraint("goal_type IN ('year','quarter','month','week','day')", name="ck_business_goals_type"),
        sa.CheckConstraint("status IN ('planned','active','at_risk','blocked','completed','cancelled')", name="ck_business_goals_status"),
        sa.CheckConstraint("period_end >= period_start", name="ck_business_goals_period"),
        sa.CheckConstraint("progress_percent IS NULL OR (progress_percent >= 0 AND progress_percent <= 100)", name="ck_business_goals_progress"),
    )
    for column in ("telegram_user_id", "external_id", "goal_type", "status", "period_start", "period_end", "notion_page_id"):
        op.create_index(f"ix_business_goals_{column}", "business_goals", [column])

    op.create_table(
        "qa_issues",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_code", sa.String(length=32), nullable=True),
        sa.Column("issue_type", sa.String(length=32), nullable=False, server_default="Bug"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="New"),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="Medium"),
        sa.Column("module", sa.String(length=128), nullable=True),
        sa.Column("environment", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("expected_result", sa.Text(), nullable=True),
        sa.Column("actual_result", sa.Text(), nullable=True),
        sa.Column("reproduction_steps", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("active_project_number", sa.String(length=32), nullable=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("app_version", sa.String(length=64), nullable=True),
        sa.Column("railway_deployment", sa.Text(), nullable=True),
        sa.Column("github_pr", sa.Text(), nullable=True),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("user_comment", sa.Text(), nullable=True),
        sa.Column("retest_result", sa.String(length=64), nullable=True),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notion_page_id", sa.String(length=128), nullable=True),
        sa.Column("notion_url", sa.Text(), nullable=True),
        sa.Column("fixed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("issue_code", name="uq_qa_issues_code"),
        sa.UniqueConstraint("telegram_user_id", "dedupe_key", name="uq_qa_issues_user_dedupe"),
        sa.CheckConstraint("issue_type IN ('Bug','Improvement','UX','Concern','Question','Data issue','Integration issue')", name="ck_qa_issues_type"),
        sa.CheckConstraint("status IN ('New','Need details','Confirmed','In progress','Ready for test','Testing','Verified','Closed','Rejected','Duplicate','Blocked')", name="ck_qa_issues_status"),
        sa.CheckConstraint("priority IN ('Critical','High','Medium','Low')", name="ck_qa_issues_priority"),
    )
    for column in ("telegram_user_id", "issue_code", "issue_type", "status", "priority", "module", "trace_id", "active_project_number", "kommo_lead_id", "dedupe_key", "notion_page_id", "created_at"):
        op.create_index(f"ix_qa_issues_{column}", "qa_issues", [column])

    op.create_table(
        "qa_attachments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("issue_id", sa.BigInteger(), sa.ForeignKey("qa_issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_name", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("telegram_file_id", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("drive_file_id", sa.String(length=255), nullable=True),
        sa.Column("drive_url", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("upload_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("upload_status IN ('pending','uploading','uploaded','failed','cancelled')", name="ck_qa_attachments_status"),
    )
    for column in ("issue_id", "drive_file_id", "checksum", "upload_status"):
        op.create_index(f"ix_qa_attachments_{column}", "qa_attachments", [column])


def downgrade() -> None:
    op.drop_table("qa_attachments")
    op.drop_table("qa_issues")
    op.drop_table("business_goals")
