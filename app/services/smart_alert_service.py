"""Smart Consumption Alerts Service (هشدارهای هوشمند قبل از اتمام حجم و زمان)."""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
import html

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from app.database.models.user import User
from app.database.models.subscription import Subscription
from app.core.constants import SubscriptionStatus
from app.bot.keyboards.default import get_effective_webapp_url

logger = logging.getLogger(__name__)


class SmartAlertService:
    """Sends proactive consumption and expiry alerts to customers before service cuts out."""

    TRAFFIC_ALERT_THRESHOLD = 0.80  # 80% consumed
    TIME_ALERT_DAYS = 3  # 3 days before expiration

    @classmethod
    async def process_smart_alerts(
        cls,
        session: AsyncSession,
        bot_instance: Any = None,
    ) -> Dict[str, Any]:
        """
        Scan active subscriptions and notify users when reaching 80% traffic
        or when 3 days remain before expiration.
        """
        now = datetime.now(timezone.utc)

        # Fetch active subscriptions
        res = await session.execute(
            select(Subscription).where(
                Subscription.status == SubscriptionStatus.ACTIVE
            )
        )
        active_subs = list(res.scalars().all())

        traffic_alerts_sent = 0
        time_alerts_sent = 0
        bot = bot_instance

        webapp_url = get_effective_webapp_url()

        for sub in active_subs:
            user = await session.get(User, sub.user_id)
            if not user or not user.telegram_id:
                continue

            limit_b = sub.traffic_limit_bytes or 0
            used_b = sub.traffic_used_bytes or 0

            # 1. Traffic threshold check (80% consumed)
            should_alert_traffic = False
            if limit_b > 0 and not sub.alert_traffic_sent:
                usage_ratio = used_b / limit_b
                if usage_ratio >= cls.TRAFFIC_ALERT_THRESHOLD:
                    should_alert_traffic = True

            # 2. Expiry time check (3 days or less remaining)
            should_alert_time = False
            rem_days = 0
            if sub.expires_at and not sub.alert_time_sent:
                exp = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
                diff = exp - now
                if 0 < diff.total_seconds() <= (cls.TIME_ALERT_DAYS * 86400):
                    should_alert_time = True
                    rem_days = max(1, diff.days)

            if not should_alert_traffic and not should_alert_time:
                continue

            # Init bot if needed
            if bot is None:
                try:
                    from app.bot.main import create_bot
                    bot = create_bot()
                except Exception as b_err:
                    logger.debug(f"Cannot initialize bot for smart alerts: {b_err}")
                    return {
                        "scanned": len(active_subs),
                        "traffic_alerts_sent": traffic_alerts_sent,
                        "time_alerts_sent": time_alerts_sent,
                        "error": str(b_err),
                    }

            inline_btns = []
            if webapp_url.startswith("https://") or "localhost" in webapp_url:
                inline_btns.append([
                    InlineKeyboardButton(text="📱 تمدید فوری در مینی‌اپ", web_app=WebAppInfo(url=webapp_url))
                ])
            inline_btns.append([
                InlineKeyboardButton(text="🔄 تمدید در ربات", callback_data="my_services_menu"),
                InlineKeyboardButton(text="🛒 خرید بسته ترافیک", callback_data="buy_service"),
            ])
            kb = InlineKeyboardMarkup(inline_keyboard=inline_btns)
            safe_name = html.escape(user.first_name or "کاربر گرامی")

            # Send Traffic Alert
            if should_alert_traffic:
                try:
                    limit_gb = round(limit_b / (1024**3), 1)
                    used_gb = round(used_b / (1024**3), 1)
                    rem_gb = round(max(0, limit_b - used_b) / (1024**3), 1)
                    pct = int((used_b / limit_b) * 100)

                    traffic_msg = (
                        f"⚠️ <b>هشدار مصرف حجم اینترنت ({pct}٪ مصرف شده)</b>\n\n"
                        f"سلام {safe_name} عزیز،\n"
                        f"بیش از ۸۰٪ از حجم ترافیک اشتراک شما به نشانی <code>{sub.client_email}</code> مصرف شده است.\n\n"
                        f"📊 <b>مصرف فعلی:</b> {used_gb} GB از {limit_gb} GB\n"
                        f"🔋 <b>ترافیک باقیمانده:</b> {rem_gb} GB\n\n"
                        f"جهت جلوگیری از قطع شدن ناگهانی اتصال، می‌توانید هم‌اکنون نسبت به تمدید یا شارژ بسته اقدام فرمایید: 👇"
                    )
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=traffic_msg,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                    sub.alert_traffic_sent = True
                    traffic_alerts_sent += 1
                except Exception as send_err:
                    logger.warning(f"Failed to send traffic alert to {user.telegram_id}: {send_err}")

            # Send Expiration Time Alert
            if should_alert_time:
                try:
                    exp_date_str = sub.expires_at.strftime("%Y-%m-%d %H:%M")
                    time_msg = (
                        f"⏳ <b>یادآوری: پایان مهلت اشتراک ({rem_days} روز مانده)</b>\n\n"
                        f"سلام {safe_name} عزیز،\n"
                        f"تنها <b>{rem_days} روز</b> دیگر تا پایان اعتبار زمانی اشتراک <code>{sub.client_email}</code> باقی مانده است.\n\n"
                        f"📅 <b>تاریخ انقضا:</b> {exp_date_str}\n\n"
                        f"جهت حفظ دسترسی بدون قطعی، از دکمه‌های زیر جهت تمدید اشتراک استفاده فرمایید: 👇"
                    )
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=time_msg,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                    sub.alert_time_sent = True
                    time_alerts_sent += 1
                except Exception as send_err:
                    logger.warning(f"Failed to send time alert to {user.telegram_id}: {send_err}")

        await session.commit()
        if bot_instance is None and bot is not None:
            try:
                await bot.session.close()
            except Exception:
                pass

        logger.info(
            f"SmartAlerts completed: scanned={len(active_subs)}, traffic_alerts={traffic_alerts_sent}, time_alerts={time_alerts_sent}"
        )
        return {
            "scanned": len(active_subs),
            "traffic_alerts_sent": traffic_alerts_sent,
            "time_alerts_sent": time_alerts_sent,
        }
