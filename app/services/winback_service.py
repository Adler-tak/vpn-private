"""Automated Win-Back Campaign Service (بازگرداندن مشتریان منقضی‌شده)."""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
import html

from sqlalchemy import select, and_, not_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from app.database.models.user import User
from app.database.models.subscription import Subscription
from app.database.models.marketing import WinbackLog
from app.database.models.reseller import Coupon
from app.core.constants import SubscriptionStatus
from app.bot.keyboards.default import get_effective_webapp_url

logger = logging.getLogger(__name__)


class WinbackService:
    """Manages automated retention and win-back offers for expired customers."""

    DEFAULT_COUPON_CODE = "COMEBACK35"
    DEFAULT_DISCOUNT_PERCENT = 35

    @classmethod
    def get_winback_settings(cls) -> Dict[str, Any]:
        """Fetch current dynamic win-back settings from SettingsService."""
        from app.services.settings_service import SettingsService
        enabled_str = str(SettingsService.get_value("WINBACK_ENABLED", "true")).lower()
        discount_str = str(SettingsService.get_value("WINBACK_DISCOUNT_PERCENT", str(cls.DEFAULT_DISCOUNT_PERCENT)))
        coupon_code = str(SettingsService.get_value("WINBACK_COUPON_CODE", cls.DEFAULT_COUPON_CODE) or cls.DEFAULT_COUPON_CODE).strip().upper()
        cooldown_str = str(SettingsService.get_value("WINBACK_COOLDOWN_DAYS", "30"))

        try:
            discount_percent = int(discount_str)
        except (ValueError, TypeError):
            discount_percent = cls.DEFAULT_DISCOUNT_PERCENT

        try:
            cooldown_days = int(cooldown_str)
        except (ValueError, TypeError):
            cooldown_days = 30

        return {
            "is_enabled": enabled_str in ("true", "1", "yes"),
            "discount_percent": max(1, min(100, discount_percent)),
            "coupon_code": coupon_code or cls.DEFAULT_COUPON_CODE,
            "cooldown_days": max(1, cooldown_days),
        }

    @classmethod
    async def update_winback_settings(
        cls,
        is_enabled: bool,
        discount_percent: int,
        coupon_code: str,
        session: AsyncSession,
    ) -> Dict[str, Any]:
        """Update win-back configuration and keep the database coupon synchronized."""
        from app.services.settings_service import SettingsService
        clean_code = (coupon_code or cls.DEFAULT_COUPON_CODE).strip().upper()
        clean_percent = max(1, min(100, int(discount_percent)))

        await SettingsService.set_value("WINBACK_ENABLED", "true" if is_enabled else "false", category="business", session=session)
        await SettingsService.set_value("WINBACK_DISCOUNT_PERCENT", str(clean_percent), category="business", session=session)
        await SettingsService.set_value("WINBACK_COUPON_CODE", clean_code, category="business", session=session)

        # Update or create the target coupon in database with new percentage
        res = await session.execute(select(Coupon).where(Coupon.code == clean_code))
        coupon = res.scalars().first()
        if coupon:
            coupon.discount_percent = clean_percent
            coupon.is_active = True
        else:
            coupon = Coupon(
                code=clean_code,
                discount_percent=clean_percent,
                max_uses=10000,
                current_uses=0,
                valid_until=datetime.now(timezone.utc) + timedelta(days=365),
                is_active=True,
            )
            session.add(coupon)
        await session.commit()

        logger.info(f"Win-back settings updated: enabled={is_enabled}, percent={clean_percent}%, code={clean_code}")
        return cls.get_winback_settings()

    @classmethod
    async def get_or_create_winback_coupon(
        cls,
        session: AsyncSession,
        code: str = None,
        discount_percent: int = None,
    ) -> Coupon:
        """Ensure standard win-back discount coupon exists and reflects configured discount percent."""
        settings = cls.get_winback_settings()
        target_code = (code or settings.get("coupon_code") or cls.DEFAULT_COUPON_CODE).strip().upper()
        target_discount = discount_percent if discount_percent is not None else settings.get("discount_percent", cls.DEFAULT_DISCOUNT_PERCENT)

        res = await session.execute(
            select(Coupon).where(Coupon.code == target_code)
        )
        coupon = res.scalars().first()
        if not coupon:
            coupon = Coupon(
                code=target_code,
                discount_percent=target_discount,
                max_uses=10000,
                current_uses=0,
                valid_until=datetime.now(timezone.utc) + timedelta(days=365),
                is_active=True,
            )
            session.add(coupon)
            await session.commit()
            await session.refresh(coupon)
            logger.info(f"Created default win-back coupon {target_code} ({target_discount}%)")
        else:
            if coupon.discount_percent != target_discount or not coupon.is_active:
                coupon.discount_percent = target_discount
                coupon.is_active = True
                await session.commit()
                await session.refresh(coupon)
                logger.info(f"Updated win-back coupon {target_code} to {target_discount}%")
        return coupon

    @classmethod
    async def process_winback_campaign(
        cls,
        session: AsyncSession,
        bot_instance: Any = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Scan for users whose subscriptions expired between 24h and 7d ago,
        verify they have no currently active subscription, and send them a special
        discount offer.
        """
        settings = cls.get_winback_settings()
        is_enabled = settings.get("is_enabled", True)
        coupon_code = settings.get("coupon_code", cls.DEFAULT_COUPON_CODE)
        discount_percent = settings.get("discount_percent", cls.DEFAULT_DISCOUNT_PERCENT)
        cooldown_days = settings.get("cooldown_days", 30)

        if not is_enabled and not force:
            logger.info("Winback automation is disabled and force=False; skipping.")
            return {
                "targeted": 0,
                "sent": 0,
                "failed": 0,
                "skipped": True,
                "coupon_code": coupon_code,
                "discount_percent": discount_percent,
                "message": "اتوماسیون بازگرداندن مشتریان غیرفعال است.",
            }

        now = datetime.now(timezone.utc)
        min_expired = now - timedelta(days=7)
        max_expired = now - timedelta(hours=24)

        # 1. Ensure winback coupon exists and is updated with the current percentage
        await cls.get_or_create_winback_coupon(session, code=coupon_code, discount_percent=discount_percent)

        # 2. Find subscriptions expired between 24h and 7d ago
        expired_subs_res = await session.execute(
            select(Subscription)
            .where(
                and_(
                    Subscription.expires_at >= min_expired,
                    Subscription.expires_at <= max_expired,
                    Subscription.status.in_([SubscriptionStatus.EXPIRED, SubscriptionStatus.DISABLED]),
                )
            )
            .order_by(Subscription.expires_at.desc())
        )
        expired_subs = list(expired_subs_res.scalars().all())

        checked_user_ids = set()
        targeted_users: List[User] = []

        for sub in expired_subs:
            u_id = sub.user_id
            if u_id in checked_user_ids:
                continue
            checked_user_ids.add(u_id)

            # Check if this user currently has ANY active subscription
            active_res = await session.execute(
                select(Subscription.id).where(
                    and_(
                        Subscription.user_id == u_id,
                        Subscription.status == SubscriptionStatus.ACTIVE,
                    )
                ).limit(1)
            )
            if active_res.first():
                # User already renewed or has another active service
                continue

            # Check if user already received winback within the cooldown days
            cooldown_threshold = now - timedelta(days=cooldown_days)
            log_res = await session.execute(
                select(WinbackLog.id).where(
                    and_(
                        WinbackLog.user_id == u_id,
                        WinbackLog.sent_at >= cooldown_threshold,
                    )
                ).limit(1)
            )
            if log_res.first():
                # Already received winback offer recently
                continue

            user = await session.get(User, u_id)
            if user and user.telegram_id:
                targeted_users.append(user)

        sent_count = 0
        failed_count = 0

        if not dry_run and targeted_users:
            if bot_instance is None:
                try:
                    from app.bot.main import create_bot
                    bot = create_bot()
                except Exception as b_err:
                    logger.error(f"Cannot initialize bot for winback campaign: {b_err}")
                    return {"targeted": len(targeted_users), "sent": 0, "failed": len(targeted_users), "error": str(b_err)}
            else:
                bot = bot_instance

            webapp_url = get_effective_webapp_url()
            inline_buttons = []
            if webapp_url.startswith("https://") or "localhost" in webapp_url:
                inline_buttons.append([
                    InlineKeyboardButton(text="🛍 تمدید فوری در مینی‌اپ", web_app=WebAppInfo(url=webapp_url))
                ])
            inline_buttons.append([
                InlineKeyboardButton(text="🔄 تمدید اشتراک در ربات", callback_data="my_services_menu"),
                InlineKeyboardButton(text="🛒 خرید سرویس جدید", callback_data="buy_service"),
            ])
            kb = InlineKeyboardMarkup(inline_keyboard=inline_buttons)

            for user in targeted_users:
                try:
                    safe_name = html.escape(user.first_name or "کاربر گرامی")
                    msg_text = (
                        f"👋 <b>سلام {safe_name} عزیز، دلمان برایتان تنگ شده! 💚</b>\n\n"
                        f"متوجه شدیم مدتی است اشتراک VPN شما به پایان رسیده است.\n"
                        f"برای اینکه مجدداً بدون دغدغه فیلترینگ و با نهایت سرعت به اینترنت آزاد متصل شوید، "
                        f"یک <b>هدیه ویژه {discount_percent}٪ تخفیف اختصاصی</b> برای شما فعال کردیم:\n\n"
                        f"🎟 <b>کد تخفیف اختصاصی:</b> <code>{coupon_code}</code>\n"
                        f"⏳ <b>مهلت استفاده:</b> فقط تا ۴۸ ساعت آینده\n\n"
                        f"جهت فعال‌سازی و تمدید آنی، از دکمه‌های زیر استفاده فرمایید: 👇"
                    )

                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=msg_text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )

                    # Log winback delivery
                    log_entry = WinbackLog(
                        user_id=user.id,
                        telegram_id=user.telegram_id,
                        coupon_code=coupon_code,
                        sent_at=datetime.now(timezone.utc),
                    )
                    session.add(log_entry)
                    sent_count += 1
                except Exception as send_err:
                    logger.warning(f"Failed to send winback message to telegram_id={user.telegram_id}: {send_err}")
                    failed_count += 1

            await session.commit()
            if bot_instance is None:
                await bot.session.close()

        logger.info(
            f"Winback Campaign run: {len(targeted_users)} targeted, {sent_count} sent, {failed_count} failed (dry_run={dry_run})"
        )

        return {
            "targeted": len(targeted_users),
            "sent": sent_count,
            "failed": failed_count,
            "coupon_code": coupon_code,
            "discount_percent": discount_percent,
        }
