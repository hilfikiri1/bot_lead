"""Add persistent memory, pending confirmations and integration audit for AI agent.

Revision ID: 007_unified_agent_v3
Revises: 007_operational_agent_v2
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "007_unified_agent_v3"
down_revision = "007_operational_agent_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("active_kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("active_local_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("memory_summary", sa.Text(), nullable=True),
        sa.Column("last_intent", sa.String(length=100), nullable=True),
        sa.Column("last_user_message", sa.Text(), nullable=True),
        sa.Column("last_assistant_message", sa.Text(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_agent_sessions_telegram_user_id", "agent_sessions", ["telegram_user_id"], unique=True)
    op.create_index("ix_agent_sessions_active_kommo_lead_id", "agent_sessions", ["active_kommo_lead_id"])
    op.create_index("ix_agent_sessions_active_local_lead_id", "agent_sessions", ["active_local_lead_id"])

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.BigInteger(), sa.ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="text"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=100), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_agent_messages_session_id", "agent_messages", ["session_id"])
    op.create_index("ix_agent_messages_telegram_user_id", "agent_messages", ["telegram_user_id"])
    op.create_index("ix_agent_messages_intent", "agent_messages", ["intent"])
    op.create_index("ix_agent_messages_created_at", "agent_messages", ["created_at"])

    op.create_table(
        "pending_agent_actions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("preview_text", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for column in ("telegram_user_id", "chat_id", "action_type", "status", "idempotency_key", "created_at"):
        op.create_index(f"ix_pending_agent_actions_{column}", "pending_agent_actions", [column], unique=(column == "idempotency_key"))

def downgrade() -> None:
    for column in ("created_at", "idempotency_key", "status", "action_type", "chat_id", "telegram_user_id"):
        op.drop_index(f"ix_pending_agent_actions_{column}", table_name="pending_agent_actions")
    op.drop_table("pending_agent_actions")

    for column in ("created_at", "intent", "telegram_user_id", "session_id"):
        op.drop_index(f"ix_agent_messages_{column}", table_name="agent_messages")
    op.drop_table("agent_messages")

    op.drop_index("ix_agent_sessions_active_local_lead_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_active_kommo_lead_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_telegram_user_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")
