"""B&BS operational agent v2 audit log.

Revision ID: 007_operational_agent_v2
Revises: 006_calendar_events
Create Date: 2026-07-29 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "007_operational_agent_v2"
down_revision = "006_calendar_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("service", sa.String(length=50), nullable=False),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    for column in ("service", "operation", "status", "external_id", "telegram_user_id", "created_at"):
        op.create_index(f"ix_integration_events_{column}", "integration_events", [column])


def downgrade() -> None:
    for column in ("created_at", "telegram_user_id", "external_id", "status", "operation", "service"):
        op.drop_index(f"ix_integration_events_{column}", table_name="integration_events")
    op.drop_table("integration_events")
