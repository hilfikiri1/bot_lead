"""Add catalog_jobs table for 1688 PDF generation."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_catalog_jobs"
down_revision: Union[str, None] = "006_calendar_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("product_title", sa.Text(), nullable=True),
        sa.Column("output_file", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_catalog_jobs_telegram_user_id", "catalog_jobs", ["telegram_user_id"])


def downgrade() -> None:
    op.drop_index("ix_catalog_jobs_telegram_user_id", table_name="catalog_jobs")
    op.drop_table("catalog_jobs")
