"""create catalog_jobs table

Revision ID: 0001_create_catalog_jobs
Revises:
Create Date: 2026-07-25 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_create_catalog_jobs"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("product_title", sa.Text(), nullable=True),
        sa.Column("output_file", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_catalog_jobs_telegram_user_id", "catalog_jobs", ["telegram_user_id"]
    )
    op.create_index("ix_catalog_jobs_status", "catalog_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_catalog_jobs_status", table_name="catalog_jobs")
    op.drop_index("ix_catalog_jobs_telegram_user_id", table_name="catalog_jobs")
    op.drop_table("catalog_jobs")
