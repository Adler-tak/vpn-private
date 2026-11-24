"""Add bank_cards table for Multi-Card Banking System.

Revision ID: 002_add_bank_cards
Revises: 001_initial_schema
Create Date: 2026-11-21 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '002_add_bank_cards'
down_revision: Union[str, None] = '001_initial_schema'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'bank_cards',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('card_number', sa.String(length=32), nullable=False),
        sa.Column('card_holder', sa.String(length=128), nullable=False),
        sa.Column('bank_name', sa.String(length=64), nullable=False),
        sa.Column('iban', sa.String(length=64), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_bank_cards_id', 'bank_cards', ['id'])


def downgrade() -> None:
    op.drop_index('ix_bank_cards_id', table_name='bank_cards')
    op.drop_table('bank_cards')
