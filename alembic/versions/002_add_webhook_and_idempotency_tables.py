"""Add webhook_events and idempotency_records tables

Revision ID: 002
Revises: 001
Create Date: 2024-12-30

Phase 1 GTM: Webhook reliability and billing idempotency tables
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '002_webhook_idempotency'
down_revision = '001_user_economic_state'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create webhook_events table for reliable Stripe webhook processing
    op.create_table(
        'webhook_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('stripe_event_id', sa.String(64), nullable=False, unique=True, index=True),
        sa.Column('event_type', sa.String(64), nullable=False, index=True),
        sa.Column('payload', postgresql.JSONB, nullable=False),
        
        # Processing status
        sa.Column('status', sa.String(32), default='pending', index=True),
        sa.Column('attempts', sa.Integer, default=0),
        sa.Column('max_attempts', sa.Integer, default=5),
        sa.Column('last_attempt_at', sa.DateTime(timezone=True)),
        sa.Column('next_retry_at', sa.DateTime(timezone=True), index=True),
        sa.Column('error_message', sa.String(2048)),
        
        # Result tracking
        sa.Column('processed_at', sa.DateTime(timezone=True)),
        sa.Column('result', postgresql.JSONB),
        
        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )
    
    # Create idempotency_records table for preventing duplicate billing operations
    op.create_table(
        'idempotency_records',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('idempotency_key', sa.String(64), nullable=False, unique=True, index=True),
        
        # Operation details
        sa.Column('operation', sa.String(64), nullable=False),
        sa.Column('user_id', sa.String(64), nullable=False, index=True),
        sa.Column('amount', sa.String(32)),
        
        # Result
        sa.Column('result', postgresql.JSONB),
        sa.Column('status', sa.String(32), default='completed'),
        
        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False, index=True),
    )
    
    # Create index for cleanup queries
    op.create_index(
        'ix_idempotency_records_expires_at',
        'idempotency_records',
        ['expires_at'],
        postgresql_where=sa.text("expires_at < NOW()")
    )


def downgrade() -> None:
    op.drop_table('idempotency_records')
    op.drop_table('webhook_events')
