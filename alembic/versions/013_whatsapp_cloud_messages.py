"""WhatsApp Cloud API durable inbox/outbox.

Revision ID: 013_whatsapp_cloud_messages
Revises: 012_supplier_offer_workspace
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "013_whatsapp_cloud_messages"
down_revision = "012_supplier_offer_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_cloud_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("client_message_draft_id", sa.BigInteger(), nullable=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=True),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("phone_number_id", sa.String(length=128), nullable=True),
        sa.Column("context_message_id", sa.String(length=255), nullable=True),
        sa.Column("media_json", sa.JSON(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["client_message_draft_id"], ["client_message_drafts.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("provider_message_id", name="uq_whatsapp_provider_message_id"),
        sa.UniqueConstraint(
            "client_message_draft_id", name="uq_whatsapp_client_message_draft_id"
        ),
    )
    for column in (
        "provider_message_id",
        "client_message_draft_id",
        "kommo_lead_id",
        "direction",
        "status",
        "phone",
        "phone_number_id",
        "provider_timestamp",
        "created_at",
    ):
        op.create_index(
            f"ix_whatsapp_cloud_messages_{column}", "whatsapp_cloud_messages", [column]
        )


def downgrade() -> None:
    op.drop_table("whatsapp_cloud_messages")
