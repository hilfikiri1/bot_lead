"""Phase 1 reliability, Kommo mapping and audio job status.

Revision ID: 003_phase1_reliability
Revises: 002_integration_checks
Create Date: 2026-06-29 00:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "003_phase1_reliability"
down_revision = "002_integration_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clients", sa.Column("kommo_contact_id", sa.BigInteger(), nullable=True)
    )
    op.create_index(
        "ix_clients_kommo_contact_id",
        "clients",
        ["kommo_contact_id"],
        unique=True,
    )

    op.add_column("leads", sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "leads", sa.Column("kommo_pipeline_id", sa.BigInteger(), nullable=True)
    )
    op.add_column("leads", sa.Column("kommo_status_id", sa.BigInteger(), nullable=True))
    op.add_column("leads", sa.Column("kommo_url", sa.Text(), nullable=True))
    op.create_index(
        "ix_leads_kommo_lead_id",
        "leads",
        ["kommo_lead_id"],
        unique=True,
    )

    op.add_column(
        "voice_notes",
        sa.Column(
            "processing_status",
            sa.String(length=32),
            nullable=False,
            server_default="received",
        ),
    )
    op.add_column(
        "voice_notes", sa.Column("processing_error", sa.Text(), nullable=True)
    )
    op.add_column(
        "voice_notes",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "voice_notes",
        sa.Column("processing_finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "voice_notes",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Preserve old duplicate records and their foreign keys. Only the oldest
    # record keeps the Telegram message identity; later duplicates become legacy
    # rows with a NULL message id before the unique constraint is created.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY telegram_user_id, telegram_message_id
                       ORDER BY id
                   ) AS rn
            FROM voice_notes
            WHERE telegram_user_id IS NOT NULL
              AND telegram_message_id IS NOT NULL
        )
        UPDATE voice_notes AS vn
        SET telegram_message_id = NULL
        FROM ranked
        WHERE vn.id = ranked.id AND ranked.rn > 1
        """
    )
    op.create_unique_constraint(
        "uq_voice_notes_telegram_message",
        "voice_notes",
        ["telegram_user_id", "telegram_message_id"],
    )

    op.add_column(
        "actions", sa.Column("idempotency_key", sa.String(length=255), nullable=True)
    )
    op.add_column("actions", sa.Column("error_message", sa.Text(), nullable=True))
    op.create_index(
        "ix_actions_idempotency_key",
        "actions",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_actions_idempotency_key", table_name="actions")
    op.drop_column("actions", "error_message")
    op.drop_column("actions", "idempotency_key")

    op.drop_constraint("uq_voice_notes_telegram_message", "voice_notes", type_="unique")
    op.drop_column("voice_notes", "updated_at")
    op.drop_column("voice_notes", "processing_finished_at")
    op.drop_column("voice_notes", "processing_started_at")
    op.drop_column("voice_notes", "processing_error")
    op.drop_column("voice_notes", "processing_status")

    op.drop_index("ix_leads_kommo_lead_id", table_name="leads")
    op.drop_column("leads", "kommo_url")
    op.drop_column("leads", "kommo_status_id")
    op.drop_column("leads", "kommo_pipeline_id")
    op.drop_column("leads", "kommo_lead_id")

    op.drop_index("ix_clients_kommo_contact_id", table_name="clients")
    op.drop_column("clients", "kommo_contact_id")
