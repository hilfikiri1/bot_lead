"""Add audit table for spreadsheet-to-Kommo lead renames."""

from alembic import op
import sqlalchemy as sa

revision = "005_spreadsheet_lead_mappings"
down_revision = "004_notion_integration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spreadsheet_lead_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("spreadsheet_lead_number", sa.String(length=32), nullable=False),
        sa.Column("original_product", sa.Text(), nullable=True),
        sa.Column("short_product_ru", sa.String(length=120), nullable=True),
        sa.Column("old_kommo_name", sa.Text(), nullable=True),
        sa.Column("new_kommo_name", sa.Text(), nullable=True),
        sa.Column("spreadsheet_row_number", sa.Integer(), nullable=True),
        sa.Column("matched_by", sa.String(length=32), nullable=True),
        sa.Column("matched_value_hash", sa.String(length=64), nullable=True),
        sa.Column("created_by_telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_spreadsheet_lead_mappings_kommo_lead_id",
        "spreadsheet_lead_mappings",
        ["kommo_lead_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_spreadsheet_lead_mappings_kommo_lead_id",
        table_name="spreadsheet_lead_mappings",
    )
    op.drop_table("spreadsheet_lead_mappings")
