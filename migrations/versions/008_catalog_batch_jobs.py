"""Extend catalog_jobs for Chrome extension batch PDF generation."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_catalog_batch_jobs"
down_revision: Union[str, None] = "007_catalog_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "catalog_jobs",
        sa.Column("job_type", sa.String(length=16), nullable=False, server_default="single"),
    )
    op.add_column("catalog_jobs", sa.Column("products_json", sa.Text(), nullable=True))
    op.add_column("catalog_jobs", sa.Column("product_count", sa.Integer(), nullable=True))
    op.alter_column("catalog_jobs", "telegram_user_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("catalog_jobs", "telegram_chat_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("catalog_jobs", "source_url", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.alter_column("catalog_jobs", "source_url", existing_type=sa.Text(), nullable=False)
    op.alter_column("catalog_jobs", "telegram_chat_id", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column("catalog_jobs", "telegram_user_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("catalog_jobs", "product_count")
    op.drop_column("catalog_jobs", "products_json")
    op.drop_column("catalog_jobs", "job_type")
