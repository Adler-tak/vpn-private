"""Add panel_type to panels table.

Revision ID: 004_add_panel_type
Revises: 003_add_payment_gateways
Create Date: 2026-11-22 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '004_add_panel_type'
down_revision: Union[str, None] = '003_add_payment_gateways'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("panels")]
    if "panel_type" not in columns:
        with op.batch_alter_table("panels") as batch_op:
            batch_op.add_column(
                sa.Column("panel_type", sa.String(length=32), nullable=False, server_default="3x-ui")
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns("panels")]
    if "panel_type" in columns:
        with op.batch_alter_table("panels") as batch_op:
            batch_op.drop_column("panel_type")
