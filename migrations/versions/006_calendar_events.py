"""Add calendar_events table for Google Calendar idempotency and audit."""

from alembic import op
import sqlalchemy as sa

revision = "006_calendar_events"
down_revision = "005_spreadsheet_lead_mappings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=True),
        sa.Column("external_event_url", sa.Text(), nullable=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("kommo_task_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("reminder_minutes", sa.Integer(), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_calendar_events_kommo_lead_id",
        "calendar_events",
        ["kommo_lead_id"],
    )
    op.create_index(
        "ix_calendar_events_idempotency_key",
        "calendar_events",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_calendar_events_idempotency_key", table_name="calendar_events")
    op.drop_index("ix_calendar_events_kommo_lead_id", table_name="calendar_events")
    op.drop_table("calendar_events")
