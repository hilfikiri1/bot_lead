"""Agent v5.0: Digital Operations Director foundation tables.

Revision ID: 011_agent_v5_operations
Revises: 010_agent_v4_2_workspace
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "011_agent_v5_operations"
down_revision = "010_agent_v4_2_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_key", sa.String(64), nullable=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("internal_lead_number", sa.String(16), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("source", sa.String(64), nullable=False, server_default="agent"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_project_events_idempotency_key"),
    )
    for col in ("project_key", "kommo_lead_id", "internal_lead_number", "event_type", "occurred_at", "external_id"):
        op.create_index(f"ix_project_events_{col}", "project_events", [col])

    op.create_table(
        "project_memories",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("project_key", sa.String(64), nullable=True),
        sa.Column("memory_json", sa.JSON(), nullable=True),
        sa.Column("requirements", sa.Text(), nullable=True),
        sa.Column("decisions", sa.Text(), nullable=True),
        sa.Column("promises", sa.Text(), nullable=True),
        sa.Column("missing_information", sa.Text(), nullable=True),
        sa.Column("risks", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_project_memories_kommo_lead_id", "project_memories", ["kommo_lead_id"], unique=True)
    op.create_index("ix_project_memories_project_key", "project_memories", ["project_key"])

    op.create_table(
        "lead_assessments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("grade", sa.String(8), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasons_json", sa.JSON(), nullable=True),
        sa.Column("risks_json", sa.JSON(), nullable=True),
        sa.Column("missing_data_json", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("recommended_action", sa.Text(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="rules"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_lead_assessments_kommo_lead_id", "lead_assessments", ["kommo_lead_id"])
    op.create_index("ix_lead_assessments_grade", "lead_assessments", ["grade"])
    op.create_index("ix_lead_assessments_created_at", "lead_assessments", ["created_at"])

    op.create_table(
        "next_action_states",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="missing"),
        sa.Column("waiting_on", sa.String(32), nullable=True),
        sa.Column("action_text", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responsible_user_id", sa.BigInteger(), nullable=True),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_reason", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_next_action_states_kommo_lead_id", "next_action_states", ["kommo_lead_id"], unique=True)
    for col in ("status", "waiting_on", "due_at", "responsible_user_id"):
        op.create_index(f"ix_next_action_states_{col}", "next_action_states", [col])

    op.create_table(
        "integration_operations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operation_type", sa.String(64), nullable=False),
        sa.Column("service", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_integration_operations_idempotency_key"),
    )
    for col in ("operation_type", "service", "status", "kommo_lead_id", "telegram_user_id", "correlation_id", "next_attempt_at"):
        op.create_index(f"ix_integration_operations_{col}", "integration_operations", [col])

    op.create_table(
        "user_notification_preferences",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("morning_hour", sa.Integer(), nullable=True),
        sa.Column("evening_hour", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("digest_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("plan_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("settings_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_user_notification_preferences_telegram_user_id",
        "user_notification_preferences",
        ["telegram_user_id"],
        unique=True,
    )

    op.create_table(
        "sheets_lead_links",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("sheet_row", sa.Integer(), nullable=True),
        sa.Column("internal_lead_number", sa.String(16), nullable=True),
        sa.Column("phone_normalized", sa.String(32), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_sheets_lead_links_kommo_lead_id", "sheets_lead_links", ["kommo_lead_id"], unique=True)
    for col in ("internal_lead_number", "phone_normalized", "email"):
        op.create_index(f"ix_sheets_lead_links_{col}", "sheets_lead_links", [col])

    op.create_table(
        "document_extractions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("project_artifact_id", sa.BigInteger(), nullable=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document_class", sa.String(64), nullable=True),
        sa.Column("extracted_json", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("conflicts_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for col in ("project_artifact_id", "kommo_lead_id", "content_hash"):
        op.create_index(f"ix_document_extractions_{col}", "document_extractions", [col])

    op.create_table(
        "undo_operations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("original_action_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("undo_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("reversible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for col in ("original_action_id", "telegram_user_id", "kommo_lead_id", "status"):
        op.create_index(f"ix_undo_operations_{col}", "undo_operations", [col])

    # Optional content hash for artifact dedupe (nullable, backfill-safe).
    op.add_column("project_artifacts", sa.Column("content_hash", sa.String(64), nullable=True))
    op.create_index("ix_project_artifacts_content_hash", "project_artifacts", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_project_artifacts_content_hash", table_name="project_artifacts")
    op.drop_column("project_artifacts", "content_hash")

    for table, cols in (
        ("undo_operations", ("status", "kommo_lead_id", "telegram_user_id", "original_action_id")),
        ("document_extractions", ("content_hash", "kommo_lead_id", "project_artifact_id")),
        ("sheets_lead_links", ("email", "phone_normalized", "internal_lead_number", "kommo_lead_id")),
        ("user_notification_preferences", ("telegram_user_id",)),
        (
            "integration_operations",
            ("next_attempt_at", "correlation_id", "telegram_user_id", "kommo_lead_id", "status", "service", "operation_type"),
        ),
        ("next_action_states", ("responsible_user_id", "due_at", "waiting_on", "status", "kommo_lead_id")),
        ("lead_assessments", ("created_at", "grade", "kommo_lead_id")),
        ("project_memories", ("project_key", "kommo_lead_id")),
        ("project_events", ("external_id", "occurred_at", "event_type", "internal_lead_number", "kommo_lead_id", "project_key")),
    ):
        for col in cols:
            op.drop_index(f"ix_{table}_{col}", table_name=table)
        op.drop_table(table)
