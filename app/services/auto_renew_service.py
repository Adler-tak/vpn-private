"""Auto-Renew Service (تمدید خودکار اشتراک از موجودی کیف پول)."""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
import uuid

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from app.database.models.user import User, Wallet
from app.database.models.subscription import Subscription
from app.database.models.product import Product
from app.database.models.panel import Panel, Inbound
from app.core.constants import SubscriptionStatus
from app.services.flash_sale_service import FlashSaleService
from app.bot.keyboards.default import get_effective_webapp_url

logger = logging.getLogger(__name__)


class AutoRenewService:
    """Manages automatic subscription renewals using customer's wallet balance."""

    @classmethod
    async def toggle_auto_renew(
        cls,
        subscription_id: uuid.UUID,
        user_id: uuid.UUID,
        session: AsyncSession,
    ) -> Dict[str, Any]:
        """Toggle auto-renew flag on a user's subscription."""
        sub = await session.get(Subscription, subscription_id)
        if not sub:
            return {"success": False, "message": "اشتراک مورد نظر یافت نشد."}

        if sub.user_id != user_id:
            return {"success": False, "message": "شما دسترسی به این اشتراک ندارید."}

        sub.auto_renew = not bool(sub.auto_renew)
        await session.commit()
        await session.refresh(sub)

        status_text = "فعال ✅" if sub.auto_renew else "غیرفعال ❌"
        return {
            "success": True,
            "auto_renew": sub.auto_renew,
            "message": f"تمدید خودکار از کیف پول برای این سرویس {status_text} شد.",
        }

    @classmethod
    def is_eligible_for_renewal(cls, sub: Subscription, now: datetime) -> bool:
        """Determine if a subscription is close to expiration or out of traffic."""
        if not sub.auto_renew:
            return False

        # Expiry time check (less than 24 hours left or recently expired within 48h)
        time_urgent = False
        if sub.expires_at:
            exp = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
            diff = exp - now
            if diff.total_seconds() <= 24 * 3600 and diff.total_seconds() >= -48 * 3600:
                time_urgent = True

        # Traffic check (less than 1 GB remaining or 95%+ consumed)
        traffic_urgent = False
        limit_b = sub.traffic_limit_bytes or 0
        used_b = sub.traffic_used_bytes or 0
        if limit_b > 0:
            remaining_bytes = max(0, limit_b - used_b)
            if remaining_bytes <= (1024**3) or (used_b / limit_b) >= 0.95:
                traffic_urgent = True

        return time_urgent or traffic_urgent

    @classmethod
    async def process_auto_renewals(
        cls,
        session: AsyncSession,
        bot_instance: Any = None,
    ) -> Dict[str, Any]:
        """
        Scan active/depleted subscriptions with auto_renew=True,
        check user wallet balance, renew if funds are sufficient,
        or send a warning notification if balance is insufficient.
        """
        now = datetime.now(timezone.utc)
        six_hours_ago = now - timedelta(hours=6)

        # 1. Fetch all auto_renew subscriptions
        res = await session.execute(
            select(Subscription).where(
                and_(
                    Subscription.auto_renew == True,
                    Subscription.status.in_([
                        SubscriptionStatus.ACTIVE,
                        SubscriptionStatus.EXPIRED,
                        SubscriptionStatus.DISABLED,
                    ]),
                )
            )
        )
        subs = list(res.scalars().all())

        renewed_count = 0
        failed_funds_count = 0
        skipped_count = 0

        # Lazy load products
        products_res = await session.execute(
            select(Product)
            .where(Product.is_active == True)
            .order_by(Product.sort_order.asc(), Product.price_irr.asc())
        )
        active_products = list(products_res.scalars().all())
        if not active_products:
            logger.warning("Auto-renew: No active products configured.")
            return {"scanned": len(subs), "renewed": 0, "failed_funds": 0, "skipped": len(subs)}

        bot = bot_instance

        for sub in subs:
            # Check eligibility
            if not cls.is_eligible_for_renewal(sub, now):
                skipped_count += 1
                continue

            # Cooldown check on recent attempt to prevent message flooding
            if sub.last_auto_renew_attempt:
                last_att = sub.last_auto_renew_attempt
                if last_att.tzinfo is None:
                    last_att = last_att.replace(tzinfo=timezone.utc)
                if last_att >= six_hours_ago:
                    skipped_count += 1
                    continue

            user = await session.get(User, sub.user_id)
            if not user:
                continue

            # Find best product package to renew
            # Match existing limit or pick default
            target_product = None
            if sub.traffic_limit_bytes > 0:
                sub_gb = round(sub.traffic_limit_bytes / (1024**3))
                for p in active_products:
                    if p.traffic_gb == sub_gb:
                        target_product = p
                        break
            if not target_product:
                target_product = active_products[0]

            # Price calculation with active flash sale
            base_price_toman = target_product.price_toman
            effective_price_toman = FlashSaleService.calculate_discounted_price(base_price_toman)

            # Get user wallet
            w_res = await session.execute(select(Wallet).where(Wallet.user_id == user.id))
            wallet = w_res.scalars().first()
            current_balance = wallet.balance_toman if wallet else 0

            # Init bot if needed for notifications
            if bot is None and user.telegram_id:
                try:
                    from app.bot.main import create_bot
                    bot = create_bot()
                except Exception as b_err:
                    logger.debug(f"Cannot initialize bot for auto-renew notifications: {b_err}")

            if current_balance >= effective_price_toman:
                # 1. Deduct balance
                if wallet:
                    wallet.balance_irr -= (effective_price_toman * 10)

                # 2. Add traffic and time
                add_traffic_bytes = target_product.traffic_bytes or int(target_product.traffic_gb * (1024**3))
                sub.traffic_limit_bytes = (sub.traffic_limit_bytes or 0) + add_traffic_bytes

                current_expiry = sub.expires_at or now
                if current_expiry.tzinfo is None:
                    current_expiry = current_expiry.replace(tzinfo=timezone.utc)
                base_date = max(now, current_expiry)
                sub.expires_at = base_date + timedelta(days=target_product.duration_days)
                sub.status = SubscriptionStatus.ACTIVE
                sub.last_auto_renew_attempt = now
                sub.alert_traffic_sent = False
                sub.alert_time_sent = False

                # 3. Sync with remote panel if available
                try:
                    panel = await session.get(Panel, sub.panel_id)
                    inbound = await session.get(Inbound, sub.inbound_id)
                    if panel and inbound and panel.is_active:
                        from app.integrations.xui.client import ThreeXUIClient
                        from app.integrations.xui.adapter import ThreeXUIPanelAdapter
                        client = ThreeXUIClient(
                            base_url=panel.base_url,
                            username=panel.username,
                            password=panel.password,
                            api_token=panel.api_token,
                            login_secret=panel.login_secret,
                        )
                        adapter = ThreeXUIPanelAdapter(client)
                        remote_inb = getattr(inbound, "remote_inbound_id", getattr(inbound, "panel_inbound_id", 1))
                        expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
                        await adapter.update_client(
                            inbound_id=remote_inb,
                            client_uuid=sub.client_uuid,
                            email=sub.client_email,
                            total_gb=int(sub.traffic_limit_bytes or 0),
                            expiry_time=expiry_ms,
                            enable=True,
                        )
                except Exception as p_err:
                    logger.warning(f"Could not sync auto-renewal to panel {sub.panel_id}: {p_err}")

                renewed_count += 1
                logger.info(
                    f"Auto-Renew SUCCESS: User {user.id} ({user.telegram_id}) renewed sub {sub.id} "
                    f"for {effective_price_toman} Toman. New expiry: {sub.expires_at}"
                )

                # 4. Notify user of success
                if bot and user.telegram_id:
                    try:
                        webapp_url = get_effective_webapp_url()
                        kb = None
                        if webapp_url.startswith("https://") or "localhost" in webapp_url:
                            kb = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text="📱 مشاهده سرویس در مینی‌اپ", web_app=WebAppInfo(url=webapp_url))
                            ]])
                        success_msg = (
                            f"🎉 <b>تمدید خودکار با موفقیت انجام شد!</b>\n\n"
                            f"سرویس <code>{sub.client_email}</code> به صورت خودکار از موجودی کیف پول شما تمدید گردید.\n\n"
                            f"📦 <b>بسته تمدیدی:</b> {target_product.name}\n"
                            f"💰 <b>مبلغ کسر شده:</b> {effective_price_toman:,} تومان\n"
                            f"💳 <b>موجودی باقیمانده کیف پول:</b> {(wallet.balance_toman if wallet else 0):,} تومان\n"
                            f"📅 <b>تاریخ انقضای جدید:</b> {sub.expires_at.strftime('%Y-%m-%d %H:%M')}\n\n"
                            f"<i>اتصال شما بدون قطعی و با بالاترین کیفیت ادامه دارد. 💚</i>"
                        )
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=success_msg,
                            reply_markup=kb,
                            parse_mode="HTML",
                        )
                    except Exception as s_err:
                        logger.warning(f"Failed to send auto-renew success notification: {s_err}")

            else:
                # Insufficient funds
                sub.last_auto_renew_attempt = now
                failed_funds_count += 1
                diff = effective_price_toman - current_balance

                logger.info(
                    f"Auto-Renew INSUFFICIENT FUNDS: User {user.id} ({user.telegram_id}) lacks {diff} Toman."
                )

                # Send shortage notification
                if bot and user.telegram_id:
                    try:
                        webapp_url = get_effective_webapp_url()
                        inline_btns = []
                        if webapp_url.startswith("https://") or "localhost" in webapp_url:
                            inline_btns.append([
                                InlineKeyboardButton(text="💳 شارژ کیف پول در مینی‌اپ", web_app=WebAppInfo(url=webapp_url))
                            ])
                        inline_btns.append([
                            InlineKeyboardButton(text="💳 شارژ کیف پول در ربات", callback_data="wallet_topup")
                        ])
                        kb = InlineKeyboardMarkup(inline_keyboard=inline_btns)

                        warn_msg = (
                            f"⚠️ <b>یادآوری: موجودی ناکافی برای تمدید خودکار!</b>\n\n"
                            f"اشتراک <code>{sub.client_email}</code> شما فعال برای «تمدید خودکار» است، اما موجودی کیف پول کافی نیست.\n\n"
                            f"💰 <b>مبلغ تمدید:</b> {effective_price_toman:,} تومان\n"
                            f"💳 <b>موجودی فعلی شما:</b> {current_balance:,} تومان\n"
                            f"🔴 <b>کسری موجودی:</b> {diff:,} تومان\n\n"
                            f"جهت جلوگیری از قطع شدن اینترنت، لطفاً کیف پول خود را شارژ فرمایید: 👇"
                        )
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=warn_msg,
                            reply_markup=kb,
                            parse_mode="HTML",
                        )
                    except Exception as w_err:
                        logger.warning(f"Failed to send auto-renew insufficient funds warning: {w_err}")

        await session.commit()
        if bot_instance is None and bot is not None:
            try:
                await bot.session.close()
            except Exception:
                pass

        return {
            "scanned": len(subs),
            "renewed": renewed_count,
            "failed_funds": failed_funds_count,
            "skipped": skipped_count,
        }
