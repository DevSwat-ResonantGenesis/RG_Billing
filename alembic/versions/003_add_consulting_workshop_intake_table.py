"""Add consulting_workshop_intakes table

Revision ID: 003
Revises: 002
Create Date: 2026-07-19 04:10:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'consulting_workshop_intakes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('session_id', sa.String(128), unique=True, nullable=False, index=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), index=True, nullable=True),
        sa.Column('full_name', sa.String(256), nullable=True),
        sa.Column('title', sa.String(256), nullable=True),
        sa.Column('company_name', sa.String(256), nullable=True),
        sa.Column('company_website', sa.String(512), nullable=True),
        sa.Column('company_email', sa.String(256), nullable=True),
        sa.Column('product_objective', sa.Text(), nullable=True),
        sa.Column('stripe_session_id', sa.String(128), unique=True, index=True, nullable=True),
        sa.Column('payment_completed', sa.Boolean(), default=False),
        sa.Column('payment_completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('stripe_customer_id', sa.String(64), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=sa.func.now()),
    )


def downgrade():
    op.drop_table('consulting_workshop_intakes')
