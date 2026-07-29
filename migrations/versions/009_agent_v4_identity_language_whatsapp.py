"""Agent v4 identity, client language and WhatsApp handoff audit.

Revision ID: 009_agent_v4_identity
Revises: 008_agent_v4_operations
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "009_agent_v4_identity"
down_revision = "008_agent_v4_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=24), nullable=False, server_default="manager"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("interface_language", sa.String(length=10), nullable=False, server_default="ru"),
        sa.Column("lead_access_scope", sa.String(length=24), nullable=False, server_default="assigned"),
        sa.Column("kommo_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "invited_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_agent_users_telegram_user_id", "agent_users", ["telegram_user_id"], unique=True)
    op.create_index("ix_agent_users_role", "agent_users", ["role"])
    op.create_index("ix_agent_users_status", "agent_users", ["status"])
    op.create_index("ix_agent_users_kommo_user_id", "agent_users", ["kommo_user_id"])

    op.create_table(
        "agent_invites",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("interface_language", sa.String(length=10), nullable=False, server_default="ru"),
        sa.Column("lead_access_scope", sa.String(length=24), nullable=False, server_default="assigned"),
        sa.Column("kommo_user_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column(
            "invited_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "accepted_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_agent_invites_token_hash", "agent_invites", ["token_hash"], unique=True)
    op.create_index("ix_agent_invites_status", "agent_invites", ["status"])
    op.create_index("ix_agent_invites_invited_by_user_id", "agent_invites", ["invited_by_user_id"])

    op.add_column("clients", sa.Column("communication_language", sa.String(length=10), nullable=True))
    op.add_column(
        "clients", sa.Column("communication_language_source", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "clients", sa.Column("communication_language_confidence", sa.Float(), nullable=True)
    )
    op.add_column(
        "clients", sa.Column("communication_language_set_by_user_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "clients",
        sa.Column("communication_language_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE clients
        SET communication_language = language,
            communication_language_source = 'legacy_card',
            communication_language_confidence = 0.90,
            communication_language_updated_at = now()
        WHERE language IN ('pl', 'uk', 'ru', 'en', 'de')
          AND communication_language IS NULL
        """
    )

    op.create_table(
        "client_message_drafts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("kommo_contact_id", sa.BigInteger(), nullable=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("communication_language", sa.String(length=10), nullable=False),
        sa.Column("language_source", sa.String(length=32), nullable=True),
        sa.Column("recipient", sa.String(length=255), nullable=True),
        sa.Column("client_name", sa.String(length=255), nullable=True),
        sa.Column("company", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="prepared"),
        sa.Column(
            "prepared_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "last_edited_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "sent_confirmed_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "sent_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("delivery_marker", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("delivery_error", sa.Text(), nullable=True),
        sa.Column("prepared_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_client_message_drafts_kommo_lead_id", "client_message_drafts", ["kommo_lead_id"])
    op.create_index("ix_client_message_drafts_kommo_contact_id", "client_message_drafts", ["kommo_contact_id"])
    op.create_index("ix_client_message_drafts_client_id", "client_message_drafts", ["client_id"])
    op.create_index("ix_client_message_drafts_channel", "client_message_drafts", ["channel"])
    op.create_index("ix_client_message_drafts_status", "client_message_drafts", ["status"])
    op.create_index("ix_client_message_drafts_prepared_by_user_id", "client_message_drafts", ["prepared_by_user_id"])
    op.create_index("ix_client_message_drafts_delivery_marker", "client_message_drafts", ["delivery_marker"], unique=True)

    op.add_column(
        "pending_agent_actions",
        sa.Column("approved_by_telegram_user_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "pending_agent_actions",
        sa.Column("executed_by_telegram_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_pending_agent_actions_approved_by_telegram_user_id",
        "pending_agent_actions",
        ["approved_by_telegram_user_id"],
    )
    op.create_index(
        "ix_pending_agent_actions_executed_by_telegram_user_id",
        "pending_agent_actions",
        ["executed_by_telegram_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_agent_actions_executed_by_telegram_user_id",
        table_name="pending_agent_actions",
    )
    op.drop_index(
        "ix_pending_agent_actions_approved_by_telegram_user_id",
        table_name="pending_agent_actions",
    )
    op.drop_column("pending_agent_actions", "executed_by_telegram_user_id")
    op.drop_column("pending_agent_actions", "approved_by_telegram_user_id")

    for index in (
        "ix_client_message_drafts_delivery_marker",
        "ix_client_message_drafts_prepared_by_user_id",
        "ix_client_message_drafts_status",
        "ix_client_message_drafts_channel",
        "ix_client_message_drafts_client_id",
        "ix_client_message_drafts_kommo_contact_id",
        "ix_client_message_drafts_kommo_lead_id",
    ):
        op.drop_index(index, table_name="client_message_drafts")
    op.drop_table("client_message_drafts")

    for column in (
        "communication_language_updated_at",
        "communication_language_set_by_user_id",
        "communication_language_confidence",
        "communication_language_source",
        "communication_language",
    ):
        op.drop_column("clients", column)

    op.drop_index("ix_agent_invites_invited_by_user_id", table_name="agent_invites")
    op.drop_index("ix_agent_invites_status", table_name="agent_invites")
    op.drop_index("ix_agent_invites_token_hash", table_name="agent_invites")
    op.drop_table("agent_invites")

    op.drop_index("ix_agent_users_kommo_user_id", table_name="agent_users")
    op.drop_index("ix_agent_users_status", table_name="agent_users")
    op.drop_index("ix_agent_users_role", table_name="agent_users")
    op.drop_index("ix_agent_users_telegram_user_id", table_name="agent_users")
    op.drop_table("agent_users")
