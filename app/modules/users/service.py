"""User registration, customer profile, and authentication services."""
import logging
import secrets
import uuid
from typing import Optional, Tuple
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User, Wallet
from app.database.models.subscription import Subscription
from app.database.models.order import Order
from app.database.models.reseller import Referral
from app.core.constants import UserRole, SubscriptionStatus
from app.core.config import settings

logger = logging.getLogger(__name__)


class UserService:
    """Handles Telegram user registration, profile retrieval, and role management."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def register_or_get_user(
        self,
        telegram_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        referrer_code: Optional[str] = None,
    ) -> Tuple[User, bool]:
        """Register new user on /start or return existing record."""
        res = await self.session.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()

        if user:
            # Update user info if changed
            updated = False
            if user.username != username:
                user.username = username
                updated = True
            if user.first_name != first_name:
                user.first_name = first_name
                updated = True
            if telegram_id in settings.ADMIN_TELEGRAM_IDS:
                if user.role != UserRole.SUPER_ADMIN:
                    user.role = UserRole.SUPER_ADMIN
                    updated = True
            else:
                if user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN):
                    user.role = UserRole.CUSTOMER
                    updated = True

            # If user does not have a referrer yet, link them if a valid referral code was passed
            if user.referred_by_id is None and referrer_code:
                clean_code = referrer_code.strip()
                possible_codes = [clean_code]
                if clean_code.startswith("ref_"):
                    possible_codes.append(clean_code[4:])
                    possible_codes.append("ref" + clean_code[4:])
                elif clean_code.startswith("ref"):
                    possible_codes.append(clean_code[3:])
                    possible_codes.append("ref_" + clean_code[3:])
                else:
                    possible_codes.append(f"ref_{clean_code}")
                    possible_codes.append(f"ref{clean_code}")

                ref_user_res = await self.session.execute(
                    select(User).where(User.referral_code.in_(possible_codes))
                )
                ref_user = ref_user_res.scalar_one_or_none()
                if ref_user and ref_user.telegram_id != telegram_id:  # Self-referral protection
                    user.referred_by_id = ref_user.id
                    updated = True

            if updated:
                await self.session.flush()
            return user, False

        # Determine if this user is a super admin from config
        role = UserRole.SUPER_ADMIN if telegram_id in settings.ADMIN_TELEGRAM_IDS else UserRole.CUSTOMER

        # Find referrer if code passed
        referred_by_id = None
        if referrer_code:
            clean_code = referrer_code.strip()
            possible_codes = [clean_code]
            if clean_code.startswith("ref_"):
                possible_codes.append(clean_code[4:])
                possible_codes.append("ref" + clean_code[4:])
            elif clean_code.startswith("ref"):
                possible_codes.append(clean_code[3:])
                possible_codes.append("ref_" + clean_code[3:])
            else:
                possible_codes.append(f"ref_{clean_code}")
                possible_codes.append(f"ref{clean_code}")

            ref_user_res = await self.session.execute(
                select(User).where(User.referral_code.in_(possible_codes))
            )
            ref_user = ref_user_res.scalar_one_or_none()
            if ref_user and ref_user.telegram_id != telegram_id:  # Self-referral protection
                referred_by_id = ref_user.id

        new_user = User(
            id=uuid.uuid4(),
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            role=role,
            referral_code=f"ref{secrets.token_hex(4)}",
            referred_by_id=referred_by_id,
        )
        self.session.add(new_user)
        await self.session.flush()

        # Initialize wallet
        wallet = Wallet(user_id=new_user.id, balance_irr=0)
        self.session.add(wallet)
        await self.session.flush()

        logger.info(f"Registered new user {new_user.telegram_id} (Role: {new_user.role})")
        return new_user, True

    async def get_user_profile_stats(self, user_id: uuid.UUID) -> dict:
        """Fetch summary data for customer profile view."""
        user = await self.session.get(User, user_id)
        if not user:
            return {}

        wallet_res = await self.session.execute(
            select(Wallet.balance_irr).where(Wallet.user_id == user_id)
        )
        balance_irr = wallet_res.scalar() or 0

        active_subs = (
            await self.session.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.user_id == user_id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                )
            )
        ).scalar() or 0

        total_orders = (
            await self.session.execute(
                select(func.count(Order.id)).where(Order.user_id == user_id)
            )
        ).scalar() or 0

        referral_count = (
            await self.session.execute(
                select(func.count(User.id)).where(User.referred_by_id == user_id)
            )
        ).scalar() or 0

        return {
            "telegram_id": user.telegram_id,
            "username": user.username or "نامشخص",
            "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
            "role": user.role,
            "balance_irr": balance_irr,
            "balance_toman": balance_irr // 10,
            "active_subscriptions": active_subs,
            "total_orders": total_orders,
            "referral_code": user.referral_code,
            "referrals_count": referral_count,
            "created_at": user.created_at,
        }
