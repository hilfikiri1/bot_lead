"""Kaizen daily journal and weekly reviews.

Revision ID: 014_kaizen_journal_entries
Revises: 013_whatsapp_cloud_messages
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "014_kaizen_journal_entries"
down_revision = "013_whatsapp_cloud_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kaizen_journal_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_type", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "source",
            sa.String(length=24),
            nullable=False,
            server_default="system",
        ),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column(
            "analysis",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "notion_page_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "telegram_user_id",
            "entry_type",
            "period_start",
            "period_end",
            name="uq_kaizen_journal_user_type_period",
        ),
        sa.CheckConstraint(
            "entry_type IN ('daily', 'weekly')",
            name="ck_kaizen_journal_entry_type",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'completed', 'skipped')",
            name="ck_kaizen_journal_status",
        ),
        sa.CheckConstraint(
            "source IN ('text', 'voice', 'scheduled', 'system')",
            name="ck_kaizen_journal_source",
        ),
        sa.CheckConstraint(
            "period_end >= period_start",
            name="ck_kaizen_journal_period_order",
        ),
    )
    for column in (
        "telegram_user_id",
        "entry_type",
        "period_start",
        "period_end",
        "status",
        "remind_at",
    ):
        op.create_index(
            f"ix_kaizen_journal_entries_{column}",
            "kaizen_journal_entries",
            [column],
        )


def downgrade() -> None:
    op.drop_table("kaizen_journal_entries")
