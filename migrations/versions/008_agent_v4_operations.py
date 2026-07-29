"""Agent v4: project links and AI usage tracking.

Revision ID: 008_agent_v4_operations
Revises: 007_unified_agent_v3
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "008_agent_v4_operations"
down_revision = "007_unified_agent_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_links",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_key", sa.String(length=64), nullable=False),
        sa.Column("internal_lead_number", sa.String(length=16), nullable=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("kommo_lead_name", sa.Text(), nullable=True),
        sa.Column("country_code", sa.String(length=8), nullable=True),
        sa.Column("client_name", sa.String(length=255), nullable=True),
        sa.Column("project_name", sa.String(length=255), nullable=True),
        sa.Column("notion_project_page_id", sa.String(length=64), nullable=True),
        sa.Column("notion_project_url", sa.Text(), nullable=True),
        sa.Column("drive_folder_id", sa.String(length=128), nullable=True),
        sa.Column("drive_folder_url", sa.Text(), nullable=True),
        sa.Column("drive_folder_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_project_links_project_key", "project_links", ["project_key"], unique=True)
    op.create_index("ix_project_links_internal_lead_number", "project_links", ["internal_lead_number"])
    op.create_index("ix_project_links_kommo_lead_id", "project_links", ["kommo_lead_id"], unique=True)
    op.create_index("ix_project_links_status", "project_links", ["status"])

    op.create_table(
        "ai_usage_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_tokens", sa.Integer(), nullable=True),
        sa.Column("audio_minutes", sa.Numeric(10, 4), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("internal_lead_number", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for column in ("operation", "telegram_user_id", "kommo_lead_id", "created_at"):
        op.create_index(f"ix_ai_usage_events_{column}", "ai_usage_events", [column])


def downgrade() -> None:
    for column in ("created_at", "kommo_lead_id", "telegram_user_id", "operation"):
        op.drop_index(f"ix_ai_usage_events_{column}", table_name="ai_usage_events")
    op.drop_table("ai_usage_events")

    op.drop_index("ix_project_links_status", table_name="project_links")
    op.drop_index("ix_project_links_kommo_lead_id", table_name="project_links")
    op.drop_index("ix_project_links_internal_lead_number", table_name="project_links")
    op.drop_index("ix_project_links_project_key", table_name="project_links")
    op.drop_table("project_links")
