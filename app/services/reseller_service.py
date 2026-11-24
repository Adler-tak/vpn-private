"""Reseller and Wholesale management service."""
import logging
import uuid
from typing import Optional, List, Dict, Any
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.reseller import Reseller
from app.database.models.user import User, Wallet
from app.database.models.subscription import Subscription
from app.core.config import settings
from app.core.constants import UserRole

logger = logging.getLogger(__name__)


class ResellerService:
    """Service for managing wholesale partners and reseller discounts."""

    @classmethod
    async def get_reseller_discount_percent(
        cls,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> int:
        """Fetch individual reseller discount percent, with fallback to system default."""
        try:
            stmt = select(Reseller).where(
                Reseller.user_id == user_id,
                Reseller.is_active == True,
            )
            reseller = (await db.execute(stmt)).scalars().first()
            if reseller and reseller.discount_percent is not None:
                return reseller.discount_percent
        except Exception as e:
            logger.warning(f"Error fetching reseller discount for {user_id}: {e}")
        return getattr(settings, "RESELLER_DISCOUNT_PERCENT", 30)

    @classmethod
    async def get_all_resellers(cls, db: AsyncSession) -> List[Dict[str, Any]]:
        """Retrieve all registered resellers with enriched user and subscription statistics."""
        stmt = (
            select(Reseller, User, Wallet)
            .join(User, Reseller.user_id == User.id)
            .outerjoin(Wallet, Wallet.user_id == User.id)
            .order_by(Reseller.created_at.desc())
        )
        results = (await db.execute(stmt)).all()

        resellers_list = []
        for reseller, user, wallet in results:
            # Count active subscriptions owned by this user
            subs_stmt = select(func.count(Subscription.id)).where(
                Subscription.user_id == user.id,
                Subscription.status == "ACTIVE",
            )
            subs_count = (await db.execute(subs_stmt)).scalar() or 0

            balance_toman = (wallet.balance_irr // 10) if wallet and wallet.balance_irr else 0
            credit_limit_toman = (reseller.credit_limit_irr // 10) if reseller.credit_limit_irr else 0

            resellers_list.append({
                "id": str(reseller.id),
                "user_id": str(user.id),
                "telegram_id": user.telegram_id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "discount_percent": reseller.discount_percent,
                "credit_limit_toman": credit_limit_toman,
                "credit_limit_irr": reseller.credit_limit_irr,
                "wallet_balance_toman": balance_toman,
                "is_active": reseller.is_active,
                "active_subscriptions_count": subs_count,
                "created_at": reseller.created_at.isoformat() if reseller.created_at else None,
            })

        return resellers_list

    @classmethod
    async def add_or_update_reseller(
        cls,
        db: AsyncSession,
        user_identifier: str,
        discount_percent: int = 20,
        credit_limit_toman: int = 0,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        """Find user by username, telegram_id, or UUID and assign reseller role and custom discount."""
        clean_id = (user_identifier or "").strip().lstrip("@")
        if not clean_id:
            raise ValueError("شناسه کاربر الزامی است.")

        user = None
        # 1. Try telegram_id (digits)
        if clean_id.isdigit():
            user = (
                await db.execute(select(User).where(User.telegram_id == int(clean_id)))
            ).scalars().first()

        # 2. Try username
        if not user:
            user = (
                await db.execute(
                    select(User).where(func.lower(User.username) == clean_id.lower())
                )
            ).scalars().first()

        # 3. Try UUID
        if not user:
            try:
                u_uuid = uuid.UUID(clean_id)
                user = await db.get(User, u_uuid)
            except Exception:
                pass

        if not user:
            raise ValueError(f"کاربری با شناسه «{user_identifier}» یافت نشد.")

        # Ensure user role is RESELLER
        user.role = UserRole.RESELLER

        # Check existing Reseller row
        reseller_stmt = select(Reseller).where(Reseller.user_id == user.id)
        reseller = (await db.execute(reseller_stmt)).scalars().first()

        credit_limit_irr = credit_limit_toman * 10

        if not reseller:
            reseller = Reseller(
                id=uuid.uuid4(),
                user_id=user.id,
                discount_percent=discount_percent,
                credit_limit_irr=credit_limit_irr,
                is_active=is_active,
            )
            db.add(reseller)
        else:
            reseller.discount_percent = discount_percent
            reseller.credit_limit_irr = credit_limit_irr
            reseller.is_active = is_active

        await db.commit()
        await db.refresh(reseller)

        return {
            "id": str(reseller.id),
            "user_id": str(user.id),
            "telegram_id": user.telegram_id,
            "username": user.username,
            "first_name": user.first_name,
            "discount_percent": reseller.discount_percent,
            "credit_limit_toman": credit_limit_toman,
            "is_active": reseller.is_active,
        }

    @classmethod
    async def update_reseller(
        cls,
        db: AsyncSession,
        reseller_id: uuid.UUID,
        discount_percent: Optional[int] = None,
        credit_limit_toman: Optional[int] = None,
        is_active: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update existing reseller's individual discount or credit limit."""
        reseller = await db.get(Reseller, reseller_id)
        if not reseller:
            raise ValueError("رکورد نماینده یافت نشد.")

        if discount_percent is not None:
            reseller.discount_percent = max(0, min(100, int(discount_percent)))

        if credit_limit_toman is not None:
            reseller.credit_limit_irr = max(0, int(credit_limit_toman) * 10)

        if is_active is not None:
            reseller.is_active = bool(is_active)

        await db.commit()
        await db.refresh(reseller)

        user = await db.get(User, reseller.user_id)
        return {
            "id": str(reseller.id),
            "user_id": str(reseller.user_id),
            "telegram_id": user.telegram_id if user else None,
            "username": user.username if user else "",
            "discount_percent": reseller.discount_percent,
            "credit_limit_toman": reseller.credit_limit_irr // 10,
            "is_active": reseller.is_active,
        }

    @classmethod
    async def delete_reseller(cls, db: AsyncSession, reseller_id: uuid.UUID) -> bool:
        """Remove reseller privilege and demote user back to customer role."""
        reseller = await db.get(Reseller, reseller_id)
        if not reseller:
            return False

        user = await db.get(User, reseller.user_id)
        if user:
            user.role = UserRole.CUSTOMER

        await db.delete(reseller)
        await db.commit()
        return True
