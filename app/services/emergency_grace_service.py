"""Emergency Grace Period (سرویس اضطراری ۱ روزه) Service."""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Tuple
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.subscription import Subscription
from app.database.models.panel import Panel, Inbound
from app.core.constants import SubscriptionStatus

logger = logging.getLogger(__name__)

# 1 GB traffic bonus
GRACE_TRAFFIC_BYTES = 1024 * 1024 * 1024  # 1 GB
GRACE_HOURS = 24
GRACE_COOLDOWN_DAYS = 7
PRICE_PER_GB_TOMAN = 3000


class EmergencyGraceService:
    """Provides 24-hour 1GB emergency connectivity for expired/depleted subscriptions."""

    @classmethod
    def can_request_grace(cls, sub: Subscription) -> Tuple[bool, str]:
        """Check if subscription is eligible for 24h emergency grace."""
        now = datetime.now(timezone.utc)

        # Check cooldown
        if sub.last_grace_at:
            lg = sub.last_grace_at if sub.last_grace_at.tzinfo else sub.last_grace_at.replace(tzinfo=timezone.utc)
            days_since = (now - lg).total_seconds() / 86400
            if days_since < GRACE_COOLDOWN_DAYS:
                days_rem = round(GRACE_COOLDOWN_DAYS - days_since, 1)
                return False, f"شما در ۷ روز اخیر از اتصال اضطراری استفاده کرده‌اید. فرصت بعدی: {days_rem} روز دیگر."

        # Check if actually expired or depleted
        is_expired = False
        if sub.expires_at:
            exp = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
            if exp <= now:
                is_expired = True

        is_depleted = False
        if sub.traffic_limit_bytes > 0:
            if (sub.traffic_limit_bytes - sub.traffic_used_bytes) <= (50 * 1024 * 1024):  # less than 50MB
                is_depleted = True

        if not is_expired and not is_depleted and sub.status == SubscriptionStatus.ACTIVE:
            return False, "اشتراک شما هنوز دارای حجم و زمان فعال است و نیازی به اتصال اضطراری ندارد."

        return True, ""

    @classmethod
    async def activate_emergency_grace(
        cls,
        subscription_id: uuid.UUID,
        user_id: uuid.UUID,
        session: AsyncSession,
    ) -> Dict[str, Any]:
        """Activate 24-hour, 1 GB emergency grace for the subscription."""
        sub = await session.get(Subscription, subscription_id)
        if not sub:
            return {"success": False, "message": "اشتراک مورد نظر یافت نشد."}

        if sub.user_id != user_id:
            return {"success": False, "message": "شما مالک این اشتراک نیستید."}

        can_grace, reason = cls.can_request_grace(sub)
        if not can_grace:
            return {"success": False, "message": reason}

        now = datetime.now(timezone.utc)

        # 1. Update subscription traffic & duration
        base_limit = max(sub.traffic_limit_bytes or 0, sub.traffic_used_bytes or 0)
        sub.traffic_limit_bytes = base_limit + GRACE_TRAFFIC_BYTES
        sub.expires_at = now + timedelta(hours=GRACE_HOURS)
        sub.status = SubscriptionStatus.ACTIVE
        sub.last_grace_at = now

        # 2. Sync client status in panel if applicable
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
                remote_inb = getattr(inbound, "remote_inbound_id", getattr(inbound, "panel_inbound_id", 1))
                expiry_ms = int(sub.expires_at.timestamp() * 1000)
                spec = ClientSpec(
                    id=sub.client_uuid,
                    email=sub.client_email,
                    flow="xtls-rprx-vision" if (sub.protocol or "").lower() == "vless" else "",
                    limitIp=inbound.max_clients or 2,
                    totalGB=int(sub.traffic_limit_bytes or 0),
                    expiryTime=expiry_ms,
                    enable=True,
                    subId=sub.subscription_token,
                )
                await adapter.update_client(remote_inb, sub.client_uuid, spec)
                logger.info(f"Re-enabled client {sub.client_email} in panel {panel.name} for Emergency Grace.")
        except Exception as panel_err:
            logger.error(f"Could not sync emergency grace to panel: {panel_err}")
            await session.rollback()
            return {"success": False, "message": f"خطا در برقراری اتصال با سرور جهت اعمال اتصال اضطراری: {panel_err}"}

        await session.commit()
        await session.refresh(sub)

        logger.info(
            f"Emergency Grace activated for sub {sub.id} (user {user_id}): +1 GB, +24h valid until {sub.expires_at}"
        )

        return {
            "success": True,
            "message": "اتصال اضطراری ۲۴ ساعته با موفقیت فعال شد.",
            "traffic_added_gb": 1.0,
            "duration_hours": GRACE_HOURS,
            "expires_at": sub.expires_at.isoformat(),
            "remaining_gb": sub.remaining_gb,
            "estimated_value_toman": PRICE_PER_GB_TOMAN,
        }
