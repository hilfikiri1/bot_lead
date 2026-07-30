"""Lead processing jobs: durable state machine for Facebook lead intake.

Revision ID: 016_lead_processing_jobs
Revises: 015_goals_and_qa
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "016_lead_processing_jobs"
down_revision = "015_goals_and_qa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lead_processing_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("facebook_lead_id", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("facebook_technical_tag", sa.Text(), nullable=True),
        sa.Column("original_title", sa.Text(), nullable=True),
        sa.Column("sheet_id", sa.String(length=128), nullable=True),
        sa.Column("sheet_row_number", sa.Integer(), nullable=True),
        sa.Column("sheet_row_key", sa.String(length=255), nullable=True),
        sa.Column("assigned_number", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="detected"),
        sa.Column("current_checkpoint", sa.String(length=64), nullable=True),
        sa.Column("processing_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("match_method", sa.String(length=32), nullable=True),
        sa.Column("match_score", sa.Integer(), nullable=True),
        sa.Column("match_reason", sa.String(length=64), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw_snapshot_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ai_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("edited_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("manual_candidates_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("runtime_state_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("kommo_lead_id", name="uq_lead_processing_jobs_kommo_lead_id"),
        sa.UniqueConstraint("assigned_number", name="uq_lead_processing_jobs_assigned_number"),
        sa.CheckConstraint(
            "status IN ("
            "'detected','matching','matched','number_assigned','ai_generated',"
            "'waiting_approval','applying','completed','skipped',"
            "'manual_match_required','error')",
            name="ck_lead_processing_jobs_status",
        ),
    )
    for column in ("kommo_lead_id", "facebook_lead_id", "assigned_number", "status"):
        op.create_index(f"ix_lead_processing_jobs_{column}", "lead_processing_jobs", [column])

    op.create_table(
        "lead_number_counters",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("counter_name", sa.String(length=64), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("counter_name", name="uq_lead_number_counters_name"),
    )
    op.create_index(
        "ix_lead_number_counters_counter_name", "lead_number_counters", ["counter_name"]
    )


def downgrade() -> None:
    op.drop_table("lead_number_counters")
    op.drop_table("lead_processing_jobs")
