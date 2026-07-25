"""create catalog_jobs table

Revision ID: 001_catalog_jobs
Revises:
Create Date: 2026-07-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "001_catalog_jobs"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    status_enum = sa.Enum(
        "received",
        "validating",
        "parsing",
        "downloading_images",
        "generating_content",
        "rendering_pdf",
        "completed",
        "failed",
        name="catalogjobstatus",
    )
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "catalog_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("status", status_enum, nullable=False),
        sa.Column("product_title", sa.Text(), nullable=True),
        sa.Column("output_file", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_catalog_jobs_telegram_user_id"), "catalog_jobs", ["telegram_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_catalog_jobs_telegram_user_id"), table_name="catalog_jobs")
    op.drop_table("catalog_jobs")
    sa.Enum(name="catalogjobstatus").drop(op.get_bind(), checkfirst=True)
