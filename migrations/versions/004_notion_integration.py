"""Add Notion page IDs for clients, leads, and voice notes."""

from alembic import op
import sqlalchemy as sa

revision = "004_notion_integration"
down_revision = "003_phase1_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("notion_page_id", sa.String(64), nullable=True))
    op.add_column("leads", sa.Column("notion_page_id", sa.String(64), nullable=True))
    op.add_column(
        "voice_notes", sa.Column("notion_page_id", sa.String(64), nullable=True)
    )
    op.create_index("ix_clients_notion_page_id", "clients", ["notion_page_id"])
    op.create_index("ix_leads_notion_page_id", "leads", ["notion_page_id"])
    op.create_index("ix_voice_notes_notion_page_id", "voice_notes", ["notion_page_id"])


def downgrade() -> None:
    op.drop_index("ix_voice_notes_notion_page_id", table_name="voice_notes")
    op.drop_index("ix_leads_notion_page_id", table_name="leads")
    op.drop_index("ix_clients_notion_page_id", table_name="clients")
    op.drop_column("voice_notes", "notion_page_id")
    op.drop_column("leads", "notion_page_id")
    op.drop_column("clients", "notion_page_id")
