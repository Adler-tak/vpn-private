"""Background synchronization tasks for panels, traffic, and expirations."""
import asyncio
from datetime import datetime, timezone
import logging
from sqlalchemy import select, and_
from app.workers.celery_app import celery_app
from app.database.session import AsyncSessionLocal
from app.database.models.panel import Panel, Inbound
from app.database.models.subscription import Subscription
from app.database.models.user import User
from app.integrations.xui.client import ThreeXUIClient
from app.integrations.xui.adapter import ThreeXUIPanelAdapter
from app.integrations.xui.schemas import ClientSpec
from app.core.exceptions import PanelError, PanelAuthError
from app.core.constants import SubscriptionStatus
from app.bot.main import create_bot
import httpx

logger = logging.getLogger(__name__)

# Track alerted subscriptions to avoid spamming alerts in every 60-second loop
_sent_quota_alerts: dict = {}


def _clear_quota_alert(sub_id) -> None:
    """Reset quota alert tracking when subscription traffic is renewed or reset."""
    _sent_quota_alerts.pop(str(sub_id), None)


async def _check_and_send_quota_alert(session, sub: Subscription, ratio: float) -> None:
    """Send proactive 80% or 95% quota warning to customer with direct renew and upgrade buttons."""
    level = 95 if ratio >= 0.95 else (80 if ratio >= 0.80 else 0)
    if level == 0:
        return

    sub_key = str(sub.id)
    sent_levels = _sent_quota_alerts.setdefault(sub_key, set())
    if level in sent_levels:
        return

    sent_levels.add(level)

    try:
        user = await session.get(User, sub.user_id)
        if not user or not user.telegram_id:
            return

        from app.core.config import settings
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

        bot = create_bot()
        try:
            used_gb = round(sub.traffic_used_bytes / (1024**3), 2)
            limit_gb = round(sub.traffic_limit_bytes / (1024**3), 2)
            rem_gb = round(max(0, sub.traffic_limit_bytes - sub.traffic_used_bytes) / (1024**3), 2)

            now = datetime.now(timezone.utc)
            rem_days = 0
            if sub.expires_at:
                exp = sub.expires_at if sub.expires_at.tzinfo else sub.expires_at.replace(tzinfo=timezone.utc)
                rem_days = max(0, (exp - now).days)

            webapp_url = f"{settings.SERVER_HOST}/webapp?sub_id={sub.id}&tg_id={user.telegram_id}"
            is_valid_webapp = (
                webapp_url.startswith("https://")
                or "localhost" in webapp_url
                or "127.0.0.1" in webapp_url
            )

            kb_rows = [
                [
                    InlineKeyboardButton(text="🔄 تمدید همین پلن", callback_data=f"sub_renew:{sub.id}"),
                    InlineKeyboardButton(text="⚡ تغییر و ارتقای پلن", callback_data=f"sub_upgrade:{sub.id}"),
                ]
            ]
            if is_valid_webapp:
                kb_rows.append([
                    InlineKeyboardButton(text="🛍 باز کردن در مینی‌اپ", web_app=WebAppInfo(url=webapp_url))
                ])

            alert_msg = (
                f"⚠️ <b>هشدار مصرف حجم اشتراک ({level}٪):</b>\n\n"
                f"مشترک گرامی، شما <b>{level}٪</b> از ترافیک مجاز سرویس خود را مصرف کرده‌اید!\n\n"
                f"🏷 <b>اکانت:</b> <code>{sub.client_email}</code>\n"
                f"📊 <b>ترافیک مصرفی:</b> <b>{used_gb}</b> از <b>{limit_gb} گیگابایت</b>\n"
                f"⏳ <b>حجم باقیمانده:</b> <b>{rem_gb} گیگابایت</b> ({rem_days} روز اعتبار مانده)\n\n"
                f"💡 <i>جهت جلوگیری از قطعی اینترنت، می‌توانید همین حالا سرویس خود را تمدید فرمایید یا به پلن بالاتر تغییر دهید:</i>"
            )

            await bot.send_message(
                chat_id=user.telegram_id,
                text=alert_msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            )
            logger.info(f"Sent {level}% quota alert to telegram user {user.telegram_id} for sub {sub.client_email}")
        finally:
            await bot.session.close()
    except Exception as e:
        logger.debug(f"Could not send proactive quota alert to user for sub {sub.id}: {e}")


async def _async_sync_all_traffic():
    """Iterate all active panels and synchronize real traffic for active subscriptions."""
    async with AsyncSessionLocal() as session:
        panels_res = await session.execute(select(Panel.id).where(Panel.is_active.is_(True)))
        panel_ids = list(panels_res.scalars().all())

        for panel_id in panel_ids:
            panel = await session.get(Panel, panel_id)
            if not panel or not panel.is_active:
                continue
            client = ThreeXUIClient(
                base_url=panel.base_url,
                username=panel.username,
                password=panel.password,
                api_token=panel.api_token,
                login_secret=panel.login_secret,
                timeout=12.0,
            )
            adapter = ThreeXUIPanelAdapter(client)

            try:
                # Fetch live inbound client list from 3X-UI to detect clients deleted directly on panel
                live_client_uuids = set()
                live_client_emails = set()
                live_client_sub_ids = set()
                has_live_roster = False
                try:
                    remote_inbounds = await adapter.list_inbounds()
                    if remote_inbounds:
                        for rinb in remote_inbounds:
                            clients_arr = (rinb.settings or {}).get("clients")
                            if isinstance(clients_arr, list):
                                has_live_roster = True
                                for c_obj in clients_arr:
                                    if isinstance(c_obj, dict):
                                        if c_obj.get("id"):
                                            live_client_uuids.add(str(c_obj["id"]).strip())
                                        if c_obj.get("email"):
                                            live_client_emails.add(str(c_obj["email"]).strip().lower())
                                        if c_obj.get("subId"):
                                            live_client_sub_ids.add(str(c_obj["subId"]).strip())
                except Exception as roster_err:
                    logger.debug(f"Could not fetch live inbound client roster on {panel.name}: {roster_err}")

                # Fetch all subscriptions on this panel
                subs_res = await session.execute(
                    select(Subscription).where(
                        Subscription.panel_id == panel.id,
                    )
                )
                subs = list(subs_res.scalars().all())

                for sub in subs:
                    try:
                        if has_live_roster:
                            exists_on_panel = (
                                (sub.client_uuid and sub.client_uuid.strip() in live_client_uuids)
                                or (sub.client_email and sub.client_email.strip().lower() in live_client_emails)
                                or (sub.subscription_token and sub.subscription_token.strip() in live_client_sub_ids)
                            )
                            if not exists_on_panel:
                                logger.info(
                                    f"Subscription {sub.client_email} ({sub.id}) no longer exists on 3X-UI panel {panel.name}. Removing from DB."
                                )
                                await session.delete(sub)
                                continue

                        stats = await adapter.get_client_traffic(sub.client_email)
                        if stats:
                            total_consumed = stats.up + stats.down
                            sub.traffic_used_bytes = total_consumed

                            # Check quota exhaustion (for active quota plans)
                            if (
                                sub.status == SubscriptionStatus.ACTIVE
                                and sub.traffic_limit_bytes > 0
                            ):
                                if total_consumed >= sub.traffic_limit_bytes:
                                    logger.info(f"Subscription {sub.id} quota exceeded ({total_consumed} bytes). Marking expired.")
                                    sub.status = SubscriptionStatus.EXPIRED

                                    # Disable client on panel while preserving original quota & timestamp
                                    inbound = await session.get(Inbound, sub.inbound_id)
                                    if inbound:
                                        expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
                                        spec = ClientSpec(
                                            id=sub.client_uuid,
                                            email=sub.client_email,
                                            flow="xtls-rprx-vision" if sub.protocol.lower() == "vless" else "",
                                            limitIp=2,
                                            totalGB=sub.traffic_limit_bytes,
                                            expiryTime=expiry_ms,
                                            enable=False,
                                            subId=sub.subscription_token,
                                        )
                                        await adapter.update_client(inbound.remote_inbound_id, sub.client_uuid, spec)
                                else:
                                    # Proactive 80% & 95% Quota Alerting
                                    ratio = total_consumed / sub.traffic_limit_bytes
                                    if ratio >= 0.80:
                                        await _check_and_send_quota_alert(session, sub, ratio)
                                    else:
                                        _clear_quota_alert(sub.id)
                    except (PanelError, PanelAuthError, ConnectionError, httpx.RequestError):
                        raise
                    except Exception as sub_err:
                        logger.warning(f"Error syncing sub {sub.client_email} on panel {panel.name}: {sub_err}")

                panel.last_sync_at = datetime.now(timezone.utc)
                panel.is_healthy = True
                panel.error_message = None
                await session.commit()
            except Exception as e:
                logger.error(f"Error syncing panel {panel.name}: {e}")
                try:
                    panel.is_healthy = False
                    panel.error_message = str(e)
                    await session.commit()
                except Exception as commit_err:
                    logger.error(f"Failed to record error state for panel {panel.name}: {commit_err}")
            finally:
                await client.close()


@celery_app.task
def sync_all_panels_traffic():
    """Celery entrypoint for traffic sync."""
    asyncio.run(_async_sync_all_traffic())


async def _async_check_expired():
    """Find expired subscriptions and disable on panel."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        subs_res = await session.execute(
            select(Subscription).where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.expires_at.is_not(None),
                    Subscription.expires_at < now,
                )
            )
        )
        expired_subs = list(subs_res.scalars().all())

        bot = create_bot()
        try:
            for sub in expired_subs:
                sub.status = SubscriptionStatus.EXPIRED
                panel = await session.get(Panel, sub.panel_id)
                inbound = await session.get(Inbound, sub.inbound_id)

                if panel and inbound:
                    client = ThreeXUIClient(
                        base_url=panel.base_url,
                        username=panel.username,
                        password=panel.password,
                        api_token=panel.api_token,
                        login_secret=panel.login_secret,
                        timeout=12.0,
                    )
                    adapter = ThreeXUIPanelAdapter(client)
                    try:
                        expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
                        spec = ClientSpec(
                            id=sub.client_uuid,
                            email=sub.client_email,
                            flow="xtls-rprx-vision" if sub.protocol.lower() == "vless" else "",
                            limitIp=2,
                            totalGB=sub.traffic_limit_bytes,
                            expiryTime=expiry_ms,
                            enable=False,
                            subId=sub.subscription_token,
                        )
                        await adapter.update_client(inbound.remote_inbound_id, sub.client_uuid, spec)
                    except Exception as e:
                        logger.warning(f"Failed to disable expired client {sub.client_email} on panel: {e}")
                    finally:
                        await client.close()

                # Notify user
                user = await session.get(User, sub.user_id)
                if user:
                    try:
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=f"⚠️ **سرویس شما به پایان رسید!**\n\nشناسه: `{sub.client_email}`\nجهت تمدید مجدد و جلوگیری از قطعی، از منوی ربات گزینه «تمدید سرویس» را انتخاب فرمایید.",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
        finally:
            await bot.session.close()

        await session.commit()


@celery_app.task
def check_expired_subscriptions():
    """Celery entrypoint for expiration checks."""
    asyncio.run(_async_check_expired())


async def _async_check_panels_health():
    """Ping each panel to ensure online status."""
    async with AsyncSessionLocal() as session:
        panels = (await session.execute(select(Panel))).scalars().all()
        for p in panels:
            client = ThreeXUIClient(
                base_url=p.base_url,
                username=p.username,
                password=p.password,
                api_token=p.api_token,
                login_secret=p.login_secret,
                timeout=10.0,
            )
            adapter = ThreeXUIPanelAdapter(client)
            try:
                status = await adapter.test_connection()
                p.is_healthy = status
                if status:
                    p.error_message = None
            except Exception as e:
                p.is_healthy = False
                p.error_message = str(e)
            finally:
                await client.close()
        try:
            await session.commit()
        except Exception as commit_err:
            logger.error(f"Error committing panels health status: {commit_err}")
            await session.rollback()


@celery_app.task
def check_panels_health():
    asyncio.run(_async_check_panels_health())
