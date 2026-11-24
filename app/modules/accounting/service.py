"""Double-entry accounting service with strict balanced ledger invariants."""
import logging
import uuid
from typing import List, Optional
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.accounting import AccountingAccount, AccountingEntry
from app.core.constants import (
    ACCOUNT_CASH_GATEWAY,
    ACCOUNT_CUSTOMER_WALLETS,
    ACCOUNT_VPN_REVENUE,
    ACCOUNT_GATEWAY_FEES,
    ACCOUNT_AFFILIATE_EXPENSE,
    AccountType,
)
from app.core.exceptions import VPNPrivateError

logger = logging.getLogger(__name__)


class AccountingService:
    """Manages immutable double-entry journal transactions."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def initialize_default_accounts(self):
        """Seed default chart of accounts if not present."""
        accounts = [
            (ACCOUNT_CASH_GATEWAY, "حساب وجوه نقد و درگاه‌های پرداخت", AccountType.ASSET),
            (ACCOUNT_CUSTOMER_WALLETS, "حساب تعهدات کیف پول کاربران", AccountType.LIABILITY),
            (ACCOUNT_VPN_REVENUE, "حساب درآمد حاصل از فروش سرویس‌ها", AccountType.REVENUE),
            (ACCOUNT_GATEWAY_FEES, "حساب کارمزد درگاه‌های بانکی", AccountType.EXPENSE),
            (ACCOUNT_AFFILIATE_EXPENSE, "حساب هزینه‌های بازاریابی و همکاران", AccountType.EXPENSE),
        ]
        for code, name, acct_type in accounts:
            existing = await self.session.get(AccountingAccount, code)
            if not existing:
                acct = AccountingAccount(code=code, name=name, account_type=acct_type)
                self.session.add(acct)
        await self.session.flush()

    async def create_balanced_entry(
        self,
        debit_code: str,
        credit_code: str,
        amount_irr: int,
        ref_type: str,
        ref_id: str,
        description: str,
    ) -> AccountingEntry:
        """Create a balanced double-entry record."""
        if amount_irr <= 0:
            raise VPNPrivateError(f"Accounting amount must be strictly positive: {amount_irr}")

        # Ensure accounts exist
        await self.initialize_default_accounts()

        group_id = uuid.uuid4()
        entry = AccountingEntry(
            id=uuid.uuid4(),
            transaction_group_id=group_id,
            debit_account_code=debit_code,
            credit_account_code=credit_code,
            amount_irr=amount_irr,
            reference_type=ref_type,
            reference_id=ref_id,
            description=description,
        )
        self.session.add(entry)
        await self.session.flush()
        logger.info(
            f"Ledger Entry recorded: Debit {debit_code} / Credit {credit_code} "
            f"for {amount_irr:,} IRR. Ref: {ref_type}:{ref_id}"
        )
        return entry

    async def record_wallet_topup(self, user_id: str, amount_irr: int, payment_id: str) -> AccountingEntry:
        """Top-up: Cash (Asset) increases, Wallet Liability increases."""
        return await self.create_balanced_entry(
            debit_code=ACCOUNT_CASH_GATEWAY,
            credit_code=ACCOUNT_CUSTOMER_WALLETS,
            amount_irr=amount_irr,
            ref_type="WALLET_TOPUP",
            ref_id=payment_id,
            description=f"شارژ کیف پول کاربر {user_id}",
        )

    async def record_order_purchase_from_wallet(
        self, user_id: str, order_id: str, amount_irr: int
    ) -> AccountingEntry:
        """Purchase from Wallet: Wallet Liability decreases, Sales Revenue increases."""
        return await self.create_balanced_entry(
            debit_code=ACCOUNT_CUSTOMER_WALLETS,
            credit_code=ACCOUNT_VPN_REVENUE,
            amount_irr=amount_irr,
            ref_type="ORDER_PURCHASE",
            ref_id=order_id,
            description=f"خرید سرویس با کیف پول توسط کاربر {user_id}",
        )

    async def record_order_direct_gateway(
        self, user_id: str, order_id: str, amount_irr: int, payment_id: str
    ) -> AccountingEntry:
        """Direct Gateway Purchase: Cash (Asset) increases, Sales Revenue increases."""
        return await self.create_balanced_entry(
            debit_code=ACCOUNT_CASH_GATEWAY,
            credit_code=ACCOUNT_VPN_REVENUE,
            amount_irr=amount_irr,
            ref_type="DIRECT_GATEWAY_PURCHASE",
            ref_id=order_id,
            description=f"خرید مستقیم از درگاه توسط کاربر {user_id}",
        )

    async def record_affiliate_commission(
        self, referrer_id: str, order_id: str, commission_irr: int
    ) -> AccountingEntry:
        """Commission: Marketing Expense increases, Wallet Liability to partner increases."""
        return await self.create_balanced_entry(
            debit_code=ACCOUNT_AFFILIATE_EXPENSE,
            credit_code=ACCOUNT_CUSTOMER_WALLETS,
            amount_irr=commission_irr,
            ref_type="AFFILIATE_COMMISSION",
            ref_id=order_id,
            description=f"پورسانت معرفی سفارش {order_id} برای کاربر {referrer_id}",
        )

    async def record_refund(self, user_id: str, order_id: str, amount_irr: int) -> AccountingEntry:
        """Refund: Revenue decreases, Wallet Liability increases."""
        return await self.create_balanced_entry(
            debit_code=ACCOUNT_VPN_REVENUE,
            credit_code=ACCOUNT_CUSTOMER_WALLETS,
            amount_irr=amount_irr,
            ref_type="REFUND",
            ref_id=order_id,
            description=f"استرداد وجه سفارش {order_id} به کیف پول کاربر {user_id}",
        )

    async def get_account_balance(self, account_code: str) -> int:
        """Compute the net balance of an accounting code."""
        debits_res = await self.session.execute(
            select(func.coalesce(func.sum(AccountingEntry.amount_irr), 0)).where(
                AccountingEntry.debit_account_code == account_code
            )
        )
        debits = debits_res.scalar() or 0

        credits_res = await self.session.execute(
            select(func.coalesce(func.sum(AccountingEntry.amount_irr), 0)).where(
                AccountingEntry.credit_account_code == account_code
            )
        )
        credits = credits_res.scalar() or 0

        acct = await self.session.get(AccountingAccount, account_code)
        if acct and acct.account_type in (AccountType.ASSET, AccountType.EXPENSE):
            return debits - credits
        return credits - debits
