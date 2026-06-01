"""Initial schema

Revision ID: 001_initial
Revises: 
Create Date: 2024-01-01 00:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'clients',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(255), nullable=True),
        sa.Column('phone', sa.String(50), nullable=True),
        sa.Column('email', sa.String(255), nullable=True),
        sa.Column('company', sa.String(255), nullable=True),
        sa.Column('language', sa.String(10), nullable=True),
        sa.Column('source', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'leads',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('client_id', sa.Integer(), sa.ForeignKey('clients.id'), nullable=True),
        sa.Column('product_requested', sa.Text(), nullable=True),
        sa.Column('budget', sa.String(255), nullable=True),
        sa.Column('country', sa.String(100), nullable=True),
        sa.Column('city', sa.String(100), nullable=True),
        sa.Column('status', sa.String(50), server_default='new'),
        sa.Column('priority', sa.String(20), server_default='medium'),
        sa.Column('next_action', sa.Text(), nullable=True),
        sa.Column('next_followup_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'voice_notes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('lead_id', sa.Integer(), sa.ForeignKey('leads.id'), nullable=True),
        sa.Column('telegram_user_id', sa.BigInteger(), nullable=True),
        sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
        sa.Column('audio_url', sa.Text(), nullable=True),
        sa.Column('transcript', sa.Text(), nullable=True),
        sa.Column('language', sa.String(10), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'ai_reports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('voice_note_id', sa.Integer(), sa.ForeignKey('voice_notes.id')),
        sa.Column('conversation_summary', sa.Text(), nullable=True),
        sa.Column('what_manager_said', postgresql.JSON(), nullable=True),
        sa.Column('mistakes_or_weak_points', postgresql.JSON(), nullable=True),
        sa.Column('missing_questions', postgresql.JSON(), nullable=True),
        sa.Column('recommended_next_step', sa.Text(), nullable=True),
        sa.Column('email_subject', sa.String(500), nullable=True),
        sa.Column('email_body', sa.Text(), nullable=True),
        sa.Column('whatsapp_message', sa.Text(), nullable=True),
        sa.Column('calendar_title', sa.String(500), nullable=True),
        sa.Column('calendar_description', sa.Text(), nullable=True),
        sa.Column('calendar_start_time', sa.String(50), nullable=True),
        sa.Column('calendar_duration_minutes', sa.Integer(), nullable=True),
        sa.Column('confidence_score', sa.Float(), nullable=True),
        sa.Column('needs_human_review', sa.Boolean(), server_default='true'),
        sa.Column('raw_json', postgresql.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        'actions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('lead_id', sa.Integer(), sa.ForeignKey('leads.id'), nullable=True),
        sa.Column('action_type', sa.String(100)),
        sa.Column('status', sa.String(50), server_default='pending'),
        sa.Column('payload', postgresql.JSON(), nullable=True),
        sa.Column('approved_by_user', sa.Boolean(), server_default='false'),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Indexes
    op.create_index('ix_clients_phone', 'clients', ['phone'])
    op.create_index('ix_clients_email', 'clients', ['email'])
    op.create_index('ix_leads_client_id', 'leads', ['client_id'])
    op.create_index('ix_leads_status', 'leads', ['status'])
    op.create_index('ix_voice_notes_lead_id', 'voice_notes', ['lead_id'])
    op.create_index('ix_ai_reports_voice_note_id', 'ai_reports', ['voice_note_id'])
    op.create_index('ix_actions_lead_id', 'actions', ['lead_id'])
    op.create_index('ix_actions_status', 'actions', ['status'])


def downgrade() -> None:
    op.drop_table('actions')
    op.drop_table('ai_reports')
    op.drop_table('voice_notes')
    op.drop_table('leads')
    op.drop_table('clients')
