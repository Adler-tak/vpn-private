"""Customer wallet management service with row-level locks and double-entry integration."""
import logging
import uuid
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import Wallet, User
from app.modules.accounting.service import AccountingService
from app.core.exceptions import InsufficientFundsError, VPNPrivateError

logger = logging.getLogger(__name__)


class WalletService:
    """Manages internal customer wallet balances with concurrency protection."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.accounting = AccountingService(session)

    async def get_or_create_wallet(self, user_id: uuid.UUID) -> Wallet:
        """Retrieve existing wallet or initialize a zero-balance wallet."""
        res = await self.session.execute(
            select(Wallet).where(Wallet.user_id == user_id).with_for_update()
        )
        wallet = res.scalar_one_or_none()
        if not wallet:
            wallet = Wallet(user_id=user_id, balance_irr=0)
            self.session.add(wallet)
            await self.session.flush()
        return wallet

    async def get_balance(self, user_id: uuid.UUID) -> int:
        """Get balance in IRR."""
        res = await self.session.execute(select(Wallet.balance_irr).where(Wallet.user_id == user_id))
        bal = res.scalar()
        return bal if bal is not None else 0

    async def credit(
        self,
        user_id: uuid.UUID,
        amount_irr: int,
        payment_id: str,
        reason: str = "TOPUP",
    ) -> int:
        """Credit customer wallet and write double-entry ledger."""
        if amount_irr <= 0:
            raise VPNPrivateError("Credit amount must be positive.")

        wallet = await self.get_or_create_wallet(user_id)
        wallet.balance_irr += amount_irr
        await self.session.flush()

        # Double-entry ledger based on transaction category
        if reason == "AFFILIATE_COMMISSION":
            await self.accounting.record_affiliate_commission(
                referrer_id=str(user_id),
                order_id=payment_id,
                commission_irr=amount_irr,
            )
        elif reason == "REFUND":
            await self.accounting.record_refund(
                user_id=str(user_id),
                order_id=payment_id,
                amount_irr=amount_irr,
            )
        else:
            await self.accounting.record_wallet_topup(
                user_id=str(user_id),
                amount_irr=amount_irr,
                payment_id=payment_id,
            )
        logger.info(f"Credited {amount_irr:,} IRR to user {user_id}. New balance: {wallet.balance_irr:,} IRR")
        return wallet.balance_irr

    async def debit(
        self,
        user_id: uuid.UUID,
        amount_irr: int,
        order_id: str,
        reason: str = "PURCHASE",
    ) -> int:
        """Debit customer wallet with row-level lock and overdraft check."""
        if amount_irr <= 0:
            raise VPNPrivateError("Debit amount must be positive.")

        wallet = await self.get_or_create_wallet(user_id)
        if wallet.balance_irr < amount_irr:
            raise InsufficientFundsError(
                f"موجودی کیف پول کافی نیست. موجودی فعلی: {wallet.balance_toman:,} تومان، مبلغ مورد نیاز: {amount_irr // 10:,} تومان"
            )

        wallet.balance_irr -= amount_irr
        await self.session.flush()

        # Double-entry ledger
        await self.accounting.record_order_purchase_from_wallet(
            user_id=str(user_id),
            order_id=order_id,
            amount_irr=amount_irr,
        )
        logger.info(f"Debited {amount_irr:,} IRR from user {user_id}. New balance: {wallet.balance_irr:,} IRR")
        return wallet.balance_irr

    async def deduct(
        self,
        user_id: uuid.UUID,
        amount_irr: int,
        order_id: str,
        reason: str = "PURCHASE",
    ) -> int:
        """Alias for debit to support deduct naming convention."""
        return await self.debit(user_id=user_id, amount_irr=amount_irr, order_id=order_id, reason=reason)
