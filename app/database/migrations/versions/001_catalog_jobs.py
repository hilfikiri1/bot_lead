from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001_catalog_jobs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    status = postgresql.ENUM("received", "validating", "parsing", "downloading_images", "generating_content", "rendering_pdf", "completed", "failed", name="catalog_job_status")
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "catalog_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("product_title", sa.Text(), nullable=True),
        sa.Column("output_file", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_catalog_jobs_telegram_user_id", "catalog_jobs", ["telegram_user_id"])
    op.create_index("ix_catalog_jobs_status", "catalog_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_catalog_jobs_status", table_name="catalog_jobs")
    op.drop_index("ix_catalog_jobs_telegram_user_id", table_name="catalog_jobs")
    op.drop_table("catalog_jobs")
    postgresql.ENUM(name="catalog_job_status").drop(op.get_bind(), checkfirst=True)
