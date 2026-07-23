"""Change reference_id from UUID to String in CreditTransaction

Revision ID: 004
Revises: 003
Create Date: 2026-07-21 01:55:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade():
    # Change reference_id from UUID to String(128)
    op.execute('ALTER TABLE credit_transactions ALTER COLUMN reference_id TYPE VARCHAR(128)')
    op.execute('ALTER TABLE credit_transactions ALTER COLUMN reference_id DROP NOT NULL')


def downgrade():
    # Revert back to UUID
    op.execute('ALTER TABLE credit_transactions ALTER COLUMN reference_id TYPE UUID USING reference_id::UUID')
    op.execute('ALTER TABLE credit_transactions ALTER COLUMN reference_id SET NOT NULL')
