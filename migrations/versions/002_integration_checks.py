"""Add integration_checks table

Revision ID: 002_integration_checks
Revises: 001_initial
Create Date: 2024-01-02 00:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '002_integration_checks'
down_revision = '001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'integration_checks',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('integration_name', sa.String(50), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('account_id', sa.BigInteger(), nullable=True),
        sa.Column('account_name', sa.Text(), nullable=True),
        sa.Column('details', postgresql.JSONB(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('telegram_user_id', sa.BigInteger(), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_integration_checks_name', 'integration_checks', ['integration_name'])
    op.create_index('ix_integration_checks_checked_at', 'integration_checks', ['checked_at'])


def downgrade() -> None:
    op.drop_index('ix_integration_checks_checked_at', table_name='integration_checks')
    op.drop_index('ix_integration_checks_name', table_name='integration_checks')
    op.drop_table('integration_checks')
