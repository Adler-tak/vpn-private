"""Initial schema migration: all normalized tables, indexes, and constraints.

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-10-31 18:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Users
    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=True),
        sa.Column('first_name', sa.String(length=128), nullable=True),
        sa.Column('last_name', sa.String(length=128), nullable=True),
        sa.Column('role', sa.String(length=32), nullable=False, server_default='customer'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('is_banned', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('referral_code', sa.String(length=32), nullable=False),
        sa.Column('referred_by_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_users_telegram_id', 'users', ['telegram_id'], unique=True)
    op.create_index('ix_users_referral_code', 'users', ['referral_code'], unique=True)

    # 2. Wallets
    op.create_table(
        'wallets',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('balance_irr', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_wallets_user_id', 'wallets', ['user_id'], unique=True)

    # 3. Panels
    op.create_table(
        'panels',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False, unique=True),
        sa.Column('base_url', sa.String(length=255), nullable=False),
        sa.Column('username', sa.String(length=128), nullable=False),
        sa.Column('password_encrypted', sa.Text(), nullable=False),
        sa.Column('api_token_encrypted', sa.Text(), nullable=True),
        sa.Column('version', sa.String(length=32), nullable=False, server_default='v3.11'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('is_healthy', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # 4. Inbounds
    op.create_table(
        'inbounds',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('panel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('panels.id', ondelete='CASCADE'), nullable=False),
        sa.Column('remote_inbound_id', sa.Integer(), nullable=False),
        sa.Column('protocol', sa.String(length=32), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('listen', sa.String(length=64), nullable=False, server_default='0.0.0.0'),
        sa.Column('tag', sa.String(length=128), nullable=True),
        sa.Column('remark', sa.String(length=255), nullable=True),
        sa.Column('stream_settings', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('client_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_clients', sa.Integer(), nullable=False, server_default='500'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_inbounds_panel_id', 'inbounds', ['panel_id'])

    # 5. Servers
    op.create_table(
        'servers',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False, unique=True),
        sa.Column('location_code', sa.String(length=16), nullable=False),
        sa.Column('location_name', sa.String(length=64), nullable=False),
        sa.Column('flag_emoji', sa.String(length=16), nullable=False, server_default='🌐'),
        sa.Column('priority', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('panel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('panels.id'), nullable=False),
        sa.Column('subscription_host', sa.String(length=255), nullable=False),
        sa.Column('subscription_port', sa.Integer(), nullable=False, server_default='2096'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('capacity_limit', sa.Integer(), nullable=False, server_default='1000'),
        sa.Column('current_clients', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_servers_location_code', 'servers', ['location_code'])

    # 6. Products
    op.create_table(
        'products',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('traffic_bytes', sa.BigInteger(), nullable=False),
        sa.Column('duration_days', sa.Integer(), nullable=False),
        sa.Column('price_irr', sa.BigInteger(), nullable=False),
        sa.Column('protocol', sa.String(length=32), nullable=False, server_default='vless'),
        sa.Column('allowed_locations', sa.String(length=255), nullable=False, server_default='ALL'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('renewal_discount_percent', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # 7. Coupons
    op.create_table(
        'coupons',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('code', sa.String(length=64), nullable=False, unique=True),
        sa.Column('discount_percent', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('discount_amount_irr', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('max_uses', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('current_uses', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # 8. Orders
    op.create_table(
        'orders',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('product_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('products.id'), nullable=False),
        sa.Column('server_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('servers.id'), nullable=True),
        sa.Column('total_price_irr', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='CREATED'),
        sa.Column('idempotency_key', sa.String(length=128), unique=True, nullable=True),
        sa.Column('coupon_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('coupons.id'), nullable=True),
        sa.Column('provisioning_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_orders_user_id', 'orders', ['user_id'])
    op.create_index('ix_orders_status', 'orders', ['status'])

    # 9. Subscriptions
    op.create_table(
        'subscriptions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('order_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('orders.id'), unique=True, nullable=True),
        sa.Column('server_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('servers.id'), nullable=False),
        sa.Column('panel_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('panels.id'), nullable=False),
        sa.Column('inbound_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('inbounds.id'), nullable=False),
        sa.Column('client_uuid', sa.String(length=64), nullable=False),
        sa.Column('client_email', sa.String(length=128), unique=True, nullable=False),
        sa.Column('protocol', sa.String(length=32), nullable=False, server_default='vless'),
        sa.Column('subscription_token', sa.String(length=64), unique=True, nullable=False),
        sa.Column('traffic_limit_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('traffic_used_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='ACTIVE'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_subscriptions_user_id', 'subscriptions', ['user_id'])
    op.create_index('ix_subscriptions_status', 'subscriptions', ['status'])

    # 10. Payments
    op.create_table(
        'payments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('order_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('orders.id'), nullable=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('amount_irr', sa.BigInteger(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('tracking_code', sa.String(length=128), unique=True, nullable=True),
        sa.Column('reference_id', sa.String(length=128), nullable=True),
        sa.Column('admin_id', sa.BigInteger(), nullable=True),
        sa.Column('admin_notes', sa.Text(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_payments_user_id', 'payments', ['user_id'])
    op.create_index('ix_payments_status', 'payments', ['status'])

    # 11. Payment Receipts
    op.create_table(
        'payment_receipts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('payment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('payments.id', ondelete='CASCADE'), unique=True, nullable=False),
        sa.Column('telegram_file_id', sa.String(length=255), nullable=True),
        sa.Column('local_file_path', sa.String(length=255), nullable=True),
        sa.Column('transaction_ref', sa.String(length=128), nullable=True),
        sa.Column('card_last_4', sa.String(length=8), nullable=True),
        sa.Column('submitted_amount_toman', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # 12. Accounting Accounts
    op.create_table(
        'accounting_accounts',
        sa.Column('code', sa.String(length=32), primary_key=True),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('account_type', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # 13. Accounting Entries
    op.create_table(
        'accounting_entries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('transaction_group_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('debit_account_code', sa.String(length=32), sa.ForeignKey('accounting_accounts.code'), nullable=False),
        sa.Column('credit_account_code', sa.String(length=32), sa.ForeignKey('accounting_accounts.code'), nullable=False),
        sa.Column('amount_irr', sa.BigInteger(), nullable=False),
        sa.Column('reference_type', sa.String(length=64), nullable=False),
        sa.Column('reference_id', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_accounting_entries_tx_group', 'accounting_entries', ['transaction_group_id'])

    # 14. Referrals
    op.create_table(
        'referrals',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('referrer_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('referred_user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), unique=True, nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('total_commission_irr', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # 15. Support Tickets & Messages
    op.create_table(
        'support_tickets',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=False, server_default='general'),
        sa.Column('priority', sa.String(length=32), nullable=False, server_default='MEDIUM'),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='OPEN'),
        sa.Column('assigned_admin_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'support_messages',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('ticket_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('support_tickets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sender_type', sa.String(length=32), nullable=False),
        sa.Column('sender_id', sa.BigInteger(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('attachment_file_id', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )

    # 16. Audit Logs & Idempotency Records
    op.create_table(
        'audit_logs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('actor_id', sa.BigInteger(), nullable=True),
        sa.Column('actor_role', sa.String(length=32), nullable=False, server_default='SYSTEM'),
        sa.Column('action', sa.String(length=128), nullable=False),
        sa.Column('target_type', sa.String(length=64), nullable=True),
        sa.Column('target_id', sa.String(length=128), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'idempotency_records',
        sa.Column('key', sa.String(length=128), primary_key=True),
        sa.Column('handler_name', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PROCESSING'),
        sa.Column('response_payload', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('idempotency_records')
    op.drop_table('audit_logs')
    op.drop_table('support_messages')
    op.drop_table('support_tickets')
    op.drop_table('referrals')
    op.drop_table('accounting_entries')
    op.drop_table('accounting_accounts')
    op.drop_table('payment_receipts')
    op.drop_table('payments')
    op.drop_table('subscriptions')
    op.drop_table('orders')
    op.drop_table('coupons')
    op.drop_table('products')
    op.drop_table('servers')
    op.drop_table('inbounds')
    op.drop_table('panels')
    op.drop_table('wallets')
    op.drop_table('users')

