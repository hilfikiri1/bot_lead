"""Add separate personal daily reflection journal type.

Revision ID: 017_reflection_journal_v2
Revises: 016_lead_processing_jobs
Create Date: 2026-08-12
"""

from alembic import op


revision = "017_reflection_journal_v2"
down_revision = "016_lead_processing_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_kaizen_journal_entry_type",
        "kaizen_journal_entries",
        type_="check",
    )
    op.create_check_constraint(
        "ck_kaizen_journal_entry_type",
        "kaizen_journal_entries",
        "entry_type IN ('daily', 'daily_personal', 'weekly')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_kaizen_journal_entry_type",
        "kaizen_journal_entries",
        type_="check",
    )
    op.create_check_constraint(
        "ck_kaizen_journal_entry_type",
        "kaizen_journal_entries",
        "entry_type IN ('daily', 'weekly')",
    )
