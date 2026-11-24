"""Downtime Compensation Service (سیستم همگانی جبران خسارت قطعی و افزودن روز هدیه)."""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Set
import html

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from app.database.models.user import User
from app.database.models.subscription import Subscription
from app.database.models.panel import Panel, Inbound
from app.core.constants import SubscriptionStatus
from app.bot.keyboards.default import get_effective_webapp_url

logger = logging.getLogger(__name__)


class CompensationService:
    """Provides bulk subscription extension and customer notification during/after outages."""

    @classmethod
    async def apply_bulk_compensation(
        cls,
        days: int,
        notify_users: bool = True,
        custom_message: Optional[str] = None,
        session: AsyncSession = None,
        bot_instance: Any = None,
    ) -> Dict[str, Any]:
        """
        Add extra days to all active subscriptions and optionally send a notification
        to all impacted customers via Telegram.
        """
        if days < 1 or days > 60:
            raise ValueError("تعداد روزهای جبرانی باید بین ۱ تا ۶۰ روز باشد.")

        now = datetime.now(timezone.utc)

        # 1. Fetch all active subscriptions
        res = await session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE
            )
        )
        active_subs = list(res.scalars().all())

        if not active_subs:
            return {
                "success": True,
                "days_added": days,
                "subscriptions_updated": 0,
                "users_notified": 0,
                "message": "هیچ اشتراک فعالی در سیستم یافت نشد.",
            }

        updated_subs_count = 0
        notified_users_count = 0
        user_ids_to_notify: Set[Any] = set()

        # Group panel clients to update
        for sub in active_subs:
            curr_exp = sub.expires_at or now
            if curr_exp.tzinfo is None:
                curr_exp = curr_exp.replace(tzinfo=timezone.utc)
            base_exp = max(now, curr_exp)
            sub.expires_at = base_exp + timedelta(days=days)
            updated_subs_count += 1
            user_ids_to_notify.add(sub.user_id)

            # Sync with remote panel if available
            try:
                panel = await session.get(Panel, sub.panel_id)
                inbound = await session.get(Inbound, sub.inbound_id)
                if panel and inbound and panel.is_active:
                    from app.integrations.xui.client import ThreeXUIClient
                    from app.integrations.xui.adapter import ThreeXUIPanelAdapter
                    from app.integrations.xui.schemas import ClientSpec
                    client = ThreeXUIClient(
                        base_url=panel.base_url,
                        username=panel.username,
                        password=panel.password,
                        api_token=panel.api_token,
                        login_secret=panel.login_secret,
                    )
                    adapter = ThreeXUIPanelAdapter(client)
                    try:
                        remote_inb = getattr(inbound, "remote_inbound_id", getattr(inbound, "panel_inbound_id", 1))
                        expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
                        spec = ClientSpec(
                            id=sub.client_uuid,
                            email=sub.client_email,
                            flow="xtls-rprx-vision" if (sub.protocol or "vless").lower() == "vless" else "",
                            limitIp=2,
                            totalGB=int(sub.traffic_limit_bytes or 0),
                            expiryTime=expiry_ms,
                            enable=True,
                            subId=sub.subscription_token,
                        )
                        await adapter.update_client(remote_inb, sub.client_uuid, spec)
                    finally:
                        await client.close()
            except Exception as p_err:
                logger.warning(f"Compensation: Could not sync sub {sub.id} to panel: {p_err}")

        # 2. Notify users if requested
        if notify_users and user_ids_to_notify:
            bot = bot_instance
            if bot is None:
                try:
                    from app.bot.main import create_bot
                    bot = create_bot()
                except Exception as b_err:
                    logger.warning(f"Cannot initialize bot for compensation notification: {b_err}")
                    bot = None

            if bot:
                webapp_url = get_effective_webapp_url()
                inline_btns = []
                if webapp_url.startswith("https://") or "localhost" in webapp_url:
                    inline_btns.append([
                        InlineKeyboardButton(text="📱 مشاهده وضعیت سرویس در مینی‌اپ", web_app=WebAppInfo(url=webapp_url))
                    ])
                inline_btns.append([
                    InlineKeyboardButton(text="📦 سرویس‌های من در ربات", callback_data="my_services_menu")
                ])
                kb = InlineKeyboardMarkup(inline_keyboard=inline_btns)

                raw_note = custom_message.strip() if custom_message else "امیدواریم همواره ارتباطی پرسرعت، امن و پایدار را با ما تجربه فرمایید."
                note = html.escape(raw_note)

                for u_id in user_ids_to_notify:
                    try:
                        u = await session.get(User, u_id)
                        if u and u.telegram_id:
                            safe_name = html.escape(u.first_name or "کاربر گرامی")
                            msg = (
                                f"🛡 <b>اطلاعیه هدیه جبران اختلال و قدردانی از همراهی شما 💚</b>\n\n"
                                f"سلام {safe_name} عزیز،\n"
                                f"به پاس شکیبایی، صبوری و وفاداری شما در روزهای اخیر، "
                                f"<b>{days} روز اشتراک رایگان</b> به تمامی سرویس‌های فعال شما افزوده گردید.\n\n"
                                f"<i>{note}</i>"
                            )
                            await bot.send_message(
                                chat_id=u.telegram_id,
                                text=msg,
                                reply_markup=kb,
                                parse_mode="HTML",
                            )
                            notified_users_count += 1
                    except Exception as send_err:
                        logger.warning(f"Compensation: Failed sending alert to user {u_id}: {send_err}")

                if bot_instance is None:
                    try:
                        await bot.session.close()
                    except Exception:
                        pass

        await session.commit()

        logger.info(
            f"Compensation applied: +{days} days to {updated_subs_count} subscriptions. {notified_users_count} users notified."
        )

        return {
            "success": True,
            "days_added": days,
            "subscriptions_updated": updated_subs_count,
            "users_notified": notified_users_count,
            "message": f"تعداد {days} روز هدیه جبرانی به {updated_subs_count} اشتراک فعال اضافه شد.",
        }
