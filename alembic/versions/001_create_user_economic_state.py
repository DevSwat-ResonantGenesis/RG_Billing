"""Create UserEconomicState table - the canonical economic authority.

Revision ID: 001_user_economic_state
Revises: 
Create Date: 2025-12-22

This migration creates the SINGLE SOURCE OF TRUTH for user economic state.
After this exists, no other service is allowed to define plans, limits, or features.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision = '001_user_economic_state'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enums first
    subscription_tier_enum = postgresql.ENUM(
        'free', 'plus', 'pro', 'enterprise',
        name='subscription_tier_enum',
        create_type=True
    )
    subscription_tier_enum.create(op.get_bind(), checkfirst=True)

    subscription_status_enum = postgresql.ENUM(
        'active', 'past_due', 'canceled', 'suspended',
        name='subscription_status_enum',
        create_type=True
    )
    subscription_status_enum.create(op.get_bind(), checkfirst=True)

    subscription_source_enum = postgresql.ENUM(
        'internal', 'stripe',
        name='subscription_source_enum',
        create_type=True
    )
    subscription_source_enum.create(op.get_bind(), checkfirst=True)

    enforcement_mode_enum = postgresql.ENUM(
        'strict', 'warn', 'off',
        name='enforcement_mode_enum',
        create_type=True
    )
    enforcement_mode_enum.create(op.get_bind(), checkfirst=True)

    # Create the table
    op.create_table(
        'user_economic_states',
        # Primary key
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        
        # Identity
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False, unique=True, index=True),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        
        # Subscription
        sa.Column('subscription_tier', subscription_tier_enum, nullable=False, server_default='free'),
        sa.Column('subscription_status', subscription_status_enum, nullable=False, server_default='active'),
        sa.Column('subscription_source', subscription_source_enum, nullable=False, server_default='internal'),
        sa.Column('subscription_id', sa.String(64), nullable=True),
        
        # Credits
        sa.Column('credit_balance', sa.Integer(), nullable=False, server_default='1000'),
        sa.Column('credit_rate', sa.Float(), nullable=False, server_default='1.0'),
        
        # Limits and features (JSONB for flexibility)
        sa.Column('hard_limits', postgresql.JSONB(), nullable=False, server_default='{"max_agents": 3, "max_workflows": 5, "max_memory_mb": 100, "max_requests_per_day": 1000, "max_tokens_per_day": 50000}'),
        sa.Column('soft_limits', postgresql.JSONB(), nullable=False, server_default='{"max_concurrent_agents": 1, "max_concurrent_workflows": 2, "max_context_tokens": 4000}'),
        sa.Column('features', postgresql.JSONB(), nullable=False, server_default='{"ide_access": false, "agent_marketplace": true, "workflow_builder": true, "code_execution": false, "api_access": true, "blockchain_access": false, "hash_sphere_access": true}'),
        
        # Enforcement
        sa.Column('enforcement_mode', enforcement_mode_enum, nullable=False, server_default='strict'),
        sa.Column('is_dev_override', sa.Boolean(), nullable=False, server_default='false'),
        
        # Audit
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now(), nullable=True),
        
        # Constraints
        sa.CheckConstraint('credit_balance >= 0', name='credit_balance_non_negative'),
        sa.CheckConstraint('credit_rate >= 0', name='credit_rate_non_negative'),
    )

    # Create index for fast lookups by org
    op.create_index('ix_user_economic_states_org_id', 'user_economic_states', ['org_id'])


def downgrade() -> None:
    op.drop_table('user_economic_states')
    
    # Drop enums
    op.execute('DROP TYPE IF EXISTS subscription_tier_enum')
    op.execute('DROP TYPE IF EXISTS subscription_status_enum')
    op.execute('DROP TYPE IF EXISTS subscription_source_enum')
    op.execute('DROP TYPE IF EXISTS enforcement_mode_enum')
