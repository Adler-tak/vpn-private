"""Automated customer notification tasks."""
import asyncio
from datetime import datetime, timezone
import logging
from sqlalchemy import select, and_
from app.workers.celery_app import celery_app
from app.database.session import AsyncSessionLocal
from app.database.models.subscription import Subscription
from app.database.models.user import User
from app.core.constants import SubscriptionStatus
from app.bot.main import create_bot

logger = logging.getLogger(__name__)


async def _async_send_quota_warnings():
    """Notify users reaching 80% or 90% quota consumption."""
    async with AsyncSessionLocal() as session:
        subs_res = await session.execute(
            select(Subscription).where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.traffic_limit_bytes > 0,
                )
            )
        )
        subs = list(subs_res.scalars().all())
        bot = create_bot()

        try:
            for sub in subs:
                ratio = sub.traffic_used_bytes / sub.traffic_limit_bytes
                if 0.85 <= ratio < 0.95:
                    user = await session.get(User, sub.user_id)
                    if user:
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=f"⚠️ **هشدار مصرف ترافیک:**\nبیش از ۸۵٪ از ترافیک سرویس شما ({sub.traffic_used_gb} از {sub.traffic_limit_gb} گیگابایت) مصرف شده است.",
                            parse_mode="Markdown",
                        )
        except Exception as e:
            logger.error(f"Error sending quota warning: {e}")
        finally:
            await bot.session.close()


@celery_app.task
def send_quota_warnings():
    asyncio.run(_async_send_quota_warnings())
