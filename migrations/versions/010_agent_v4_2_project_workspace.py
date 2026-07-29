"""Agent v4.2 unified project workspace and file audit.

Revision ID: 010_agent_v4_2_workspace
Revises: 009_agent_v4_identity
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa


revision = "010_agent_v4_2_workspace"
down_revision = "009_agent_v4_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_agent_actions",
        sa.Column("batch_group_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_pending_agent_actions_batch_group_id",
        "pending_agent_actions",
        ["batch_group_id"],
    )

    op.create_table(
        "project_artifacts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_link_id",
            sa.BigInteger(),
            sa.ForeignKey("project_links.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kommo_lead_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("suggested_filename", sa.String(length=255), nullable=False),
        sa.Column("final_filename", sa.String(length=255), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("artifact_type_label", sa.String(length=128), nullable=False),
        sa.Column("classification_source", sa.String(length=32), nullable=False),
        sa.Column("subfolder_name", sa.String(length=255), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("preview_text", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("drive_file_id", sa.String(length=128), nullable=True),
        sa.Column("drive_file_url", sa.Text(), nullable=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=True),
        sa.Column("notion_page_url", sa.Text(), nullable=True),
        sa.Column("kommo_note_created", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("warnings_json", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("uploaded_by_telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "telegram_user_id",
            "telegram_message_id",
            name="uq_project_artifacts_telegram_message",
        ),
    )
    for column in (
        "project_link_id",
        "kommo_lead_id",
        "telegram_user_id",
        "telegram_message_id",
        "artifact_type",
        "status",
        "drive_file_id",
        "uploaded_by_telegram_user_id",
        "created_at",
    ):
        op.create_index(
            f"ix_project_artifacts_{column}",
            "project_artifacts",
            [column],
        )


def downgrade() -> None:
    for column in (
        "created_at",
        "uploaded_by_telegram_user_id",
        "drive_file_id",
        "status",
        "artifact_type",
        "telegram_user_id",
        "telegram_message_id",
        "kommo_lead_id",
        "project_link_id",
    ):
        op.drop_index(
            f"ix_project_artifacts_{column}",
            table_name="project_artifacts",
        )
    op.drop_table("project_artifacts")
    op.drop_index(
        "ix_pending_agent_actions_batch_group_id",
        table_name="pending_agent_actions",
    )
    op.drop_column("pending_agent_actions", "batch_group_id")
