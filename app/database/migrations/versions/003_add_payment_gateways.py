"""Add payment_gateways table and gateway_id column to payments.

Revision ID: 003_add_payment_gateways
Revises: 002_add_bank_cards
Create Date: 2026-11-21 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '003_add_payment_gateways'
down_revision: Union[str, None] = '002_add_bank_cards'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'payment_gateways',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('config', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('sort_order', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_payment_gateways_id', 'payment_gateways', ['id'])
    op.create_index('ix_payment_gateways_provider', 'payment_gateways', ['provider'])
    op.create_index('ix_payment_gateways_is_active', 'payment_gateways', ['is_active'])

    # Add gateway_id column to payments
    try:
        op.add_column('payments', sa.Column('gateway_id', postgresql.UUID(as_uuid=True), nullable=True))
        op.create_index('ix_payments_gateway_id', 'payments', ['gateway_id'])
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_index('ix_payments_gateway_id', table_name='payments')
        op.drop_column('payments', 'gateway_id')
    except Exception:
        pass
    op.drop_index('ix_payment_gateways_is_active', table_name='payment_gateways')
    op.drop_index('ix_payment_gateways_provider', table_name='payment_gateways')
    op.drop_index('ix_payment_gateways_id', table_name='payment_gateways')
    op.drop_table('payment_gateways')
