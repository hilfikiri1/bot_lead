"""Supplier/factory workspace and normalized offer comparison tables.

Revision ID: 012_supplier_offer_workspace
Revises: 011_agent_v5_operations
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "012_supplier_offer_workspace"
down_revision = "011_agent_v5_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_suppliers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("internal_lead_number", sa.String(16), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("company_name", sa.String(255), nullable=True),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("contact_name", sa.String(255), nullable=True),
        sa.Column("contact_channel", sa.String(64), nullable=True),
        sa.Column("contact_value", sa.String(500), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("verification_status", sa.String(32), nullable=False, server_default="not_checked"),
        sa.Column("verification_summary", sa.Text(), nullable=True),
        sa.Column("last_contact_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_followup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_by_telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("kommo_lead_id", "identity_key", name="uq_project_suppliers_lead_identity"),
    )
    for col in (
        "kommo_lead_id", "internal_lead_number", "platform", "status",
        "verification_status", "next_followup_at", "created_at",
    ):
        op.create_index(f"ix_project_suppliers_{col}", "project_suppliers", [col])

    op.create_table(
        "supplier_inquiries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("project_suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.String(64), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="prepared"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_summary", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_by_telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for col in ("supplier_id", "kommo_lead_id", "status", "due_at", "created_at"):
        op.create_index(f"ix_supplier_inquiries_{col}", "supplier_inquiries", [col])

    op.create_table(
        "supplier_offers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("project_suppliers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="received"),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("incoterm", sa.String(32), nullable=True),
        sa.Column("named_place", sa.String(255), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("total_price", sa.Numeric(18, 2), nullable=True),
        sa.Column("moq", sa.String(255), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("warranty_months", sa.Integer(), nullable=True),
        sa.Column("payment_terms", sa.String(500), nullable=True),
        sa.Column("packaging", sa.Text(), nullable=True),
        sa.Column("certifications_json", sa.JSON(), nullable=True),
        sa.Column("key_components_json", sa.JSON(), nullable=True),
        sa.Column("deviations_json", sa.JSON(), nullable=True),
        sa.Column("source_artifact_id", sa.BigInteger(), sa.ForeignKey("project_artifacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    for col in (
        "supplier_id", "kommo_lead_id", "status", "incoterm",
        "source_artifact_id", "created_at",
    ):
        op.create_index(f"ix_supplier_offers_{col}", "supplier_offers", [col])


def downgrade() -> None:
    for table, cols in (
        ("supplier_offers", ("created_at", "source_artifact_id", "incoterm", "status", "kommo_lead_id", "supplier_id")),
        ("supplier_inquiries", ("created_at", "due_at", "status", "kommo_lead_id", "supplier_id")),
        ("project_suppliers", ("created_at", "next_followup_at", "verification_status", "status", "platform", "internal_lead_number", "kommo_lead_id")),
    ):
        for col in cols:
            op.drop_index(f"ix_{table}_{col}", table_name=table)
        op.drop_table(table)
