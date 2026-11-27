"""Panel and Xray Health Monitoring Service.
Monitors panel reachability, 3X-UI connection, and Xray core running state.
Alerts administrators via Telegram when Xray or panel errors persist across multiple verification retries.
"""
import asyncio
from datetime import datetime, timezone
import html
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.database.models.panel import Panel
from app.integrations.xui.client import ThreeXUIClient
from app.core.exceptions import PanelError, PanelAuthError
from app.bot.notifications import notify_all_admins
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

# State cache for alert deduplication and recovery tracking
# Format: { panel_id_str: { "is_in_error": bool, "last_alert_at": datetime, "last_error": str, "panel_name": str } }
_panel_alert_cache: Dict[str, Dict[str, Any]] = {}

# Reusable client session pool to avoid hammering /login on every single ping
_panel_client_pool: Dict[str, ThreeXUIClient] = {}


class PanelHealthService:
    """Checks panel reachability & Xray status, and dispatches alerts to admins after consecutive retry verification."""

    @classmethod
    def get_cached_state(cls, panel_id: uuid.UUID) -> Optional[Dict[str, Any]]:
        """Return cached alert state for a panel."""
        return _panel_alert_cache.get(str(panel_id))

    @classmethod
    def reset_cache_for_testing(cls) -> None:
        """Reset internal alert cache and client pool (used in unit tests)."""
        _panel_alert_cache.clear()
        _panel_client_pool.clear()

    @classmethod
    def get_monitoring_config(cls) -> Dict[str, int]:
        """Fetch current monitoring intervals and retry configuration from dynamic system settings."""
        return {
            "check_interval_seconds": SettingsService.get_int("PANEL_HEALTH_CHECK_INTERVAL", 60),
            "retry_count": SettingsService.get_int("PANEL_HEALTH_RETRY_COUNT", 5),
            "retry_interval_seconds": SettingsService.get_int("PANEL_HEALTH_RETRY_INTERVAL", 5),
            "alert_cooldown_minutes": SettingsService.get_int("PANEL_HEALTH_ALERT_COOLDOWN", 30),
        }

    @classmethod
    def _get_or_create_client(cls, panel: Panel, timeout: float = 8.0) -> ThreeXUIClient:
        """Get or initialize a pooled ThreeXUIClient for the panel to preserve authenticated sessions."""
        key = str(panel.id)
        existing = _panel_client_pool.get(key)
        if existing and existing.base_url == panel.base_url and existing.username == panel.username:
            return existing

        client = ThreeXUIClient(
            base_url=panel.base_url,
            username=panel.username,
            password=panel.password,
            api_token=panel.api_token,
            login_secret=panel.login_secret,
            timeout=timeout,
        )
        _panel_client_pool[key] = client
        return client

    @classmethod
    async def check_panel_health(
        cls,
        panel: Panel,
        timeout: float = 8.0,
        use_pool: bool = True,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Check reachability and Xray core state for a panel.
        Reuses authenticated session if available to avoid 403 login rate-limits.

        Returns:
            (is_healthy: bool, error_message: str, details: dict)
        """
        if use_pool:
            client = cls._get_or_create_client(panel, timeout=timeout)
        else:
            client = ThreeXUIClient(
                base_url=panel.base_url,
                username=panel.username,
                password=panel.password,
                api_token=panel.api_token,
                login_secret=panel.login_secret,
                timeout=timeout,
            )

        try:
            # If client is not yet authenticated, perform initial login
            if not getattr(client, "_is_authenticated", False) and not client.api_token:
                ok = await client.login()
                if not ok:
                    return False, "احراز هویت با نام کاربری یا کلمه عبور ناموفق بود.", {"reason": "auth_failed"}

            # Query server status
            try:
                status_resp = await client.get_status()
            except (PanelAuthError, PanelError) as req_err:
                # If session expired, attempt one fresh login
                logger.info(f"Session expired or failed for {panel.name} ({req_err}), re-authenticating...")
                client._is_authenticated = False
                ok = await client.login()
                if not ok:
                    return False, f"احراز هویت مجدد ناموفق بود: {req_err}", {"reason": "auth_failed"}
                status_resp = await client.get_status()

            obj = status_resp.get("obj") if isinstance(status_resp, dict) else {}
            if not isinstance(obj, dict):
                obj = {}

            xray_data = obj.get("xray") if isinstance(obj.get("xray"), dict) else {}
            xray_state = str(xray_data.get("state") or "").strip().lower()
            xray_error = str(xray_data.get("errorMsg") or "").strip()
            xray_version = str(xray_data.get("version") or "").strip()

            # In 3X-UI, xray["state"] is typically "running" when healthy
            # If errorMsg is non-empty, Xray encountered an error!
            if xray_error:
                return False, f"خطای داخلی هسته Xray: {xray_error}", {
                    "xray_state": xray_state,
                    "xray_error": xray_error,
                    "xray_version": xray_version,
                    "raw": obj,
                }

            # If xray_state is explicitly not running (e.g. stop, error, down)
            if xray_state and xray_state != "running":
                return False, f"هسته Xray متوقف است (وضعیت: {xray_state})", {
                    "xray_state": xray_state,
                    "xray_version": xray_version,
                    "raw": obj,
                }

            return True, "", {
                "xray_state": xray_state or "running",
                "xray_version": xray_version,
                "raw": obj,
            }
        except PanelAuthError as pae:
            client._is_authenticated = False
            return False, f"خطای احراز هویت با پنل: {pae}", {"reason": "auth_error"}
        except PanelError as pe:
            return False, f"خطای پنل 3X-UI: {pe}", {"reason": "panel_error"}
        except Exception as exc:
            client._is_authenticated = False
            return False, f"عدم پاسخگویی یا قطع ارتباط با سرور: {exc}", {"reason": "connection_error"}
        finally:
            if not use_pool:
                await client.close()

    @classmethod
    async def check_panel_health_with_retry(
        cls,
        panel: Panel,
        retry_count: Optional[int] = None,
        retry_interval: Optional[int] = None,
        timeout: float = 8.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Verify panel health with consecutive retry loop.
        If an initial failure occurs, retries `retry_count` times with `retry_interval` sleep.
        Only returns False if ALL consecutive retries fail.
        """
        if retry_count is None:
            retry_count = SettingsService.get_int("PANEL_HEALTH_RETRY_COUNT", 5)
        if retry_interval is None:
            retry_interval = SettingsService.get_int("PANEL_HEALTH_RETRY_INTERVAL", 5)

        # 1. Initial attempt
        is_healthy, err_msg, details = await cls.check_panel_health(panel, timeout=timeout)
        if is_healthy:
            return True, "", details

        # 2. If initial check failed and retry_count > 0, enter retry verification cycle
        if retry_count > 0:
            logger.info(
                f"Panel '{panel.name}' failed initial check ({err_msg}). "
                f"Entering verification retry loop ({retry_count} retries every {retry_interval}s)..."
            )
            for attempt in range(1, retry_count + 1):
                await asyncio.sleep(retry_interval)
                is_retry_ok, retry_err, retry_details = await cls.check_panel_health(panel, timeout=timeout)
                if is_retry_ok:
                    logger.info(
                        f"Panel '{panel.name}' recovered on retry attempt {attempt}/{retry_count}. "
                        "False alert suppressed."
                    )
                    return True, "", retry_details
                else:
                    logger.warning(
                        f"Panel '{panel.name}' retry {attempt}/{retry_count} still failing: {retry_err}"
                    )
                    err_msg = retry_err
                    details = retry_details

            # All consecutive retries failed
            confirmed_err = f"{err_msg} (تأیید پس از {retry_count} بار تست متوالی با فاصله {retry_interval} ثانیه)"
            return False, confirmed_err, details

        return False, err_msg, details

    @classmethod
    async def record_panel_result(
        cls,
        panel: Panel,
        is_healthy: bool,
        error_msg: str,
        db_session: AsyncSession,
        bot: Optional[Bot] = None,
        details: Optional[Dict[str, Any]] = None,
        force_alert: bool = False,
    ) -> bool:
        """
        Record panel status, dispatch alerts if persistent Xray/panel error occurred,
        or send recovery notification if the panel was previously failing and recovered.

        Returns True if an alert/notification was dispatched, False otherwise.
        """
        cache_key = str(panel.id)
        now = datetime.now(timezone.utc)
        cached = _panel_alert_cache.get(cache_key)
        alert_sent = False

        panel_name = panel.name or "نامشخص"
        clean_name = html.escape(panel_name)
        clean_url = html.escape(panel.base_url or "")

        should_close_bot = False
        active_bot = bot

        cooldown_mins = SettingsService.get_int("PANEL_HEALTH_ALERT_COOLDOWN", 30)
        cooldown_seconds = max(60, cooldown_mins * 60)

        if not is_healthy:
            # Persistent error confirmed
            clean_err = html.escape(error_msg or "خطای نامشخص در سرویس Xray یا پنل")
            xray_ver = ""
            if details and details.get("xray_version"):
                xray_ver = f"\n🏷 <b>نسخه Xray:</b> <code>{html.escape(str(details.get('xray_version')))}</code>"

            should_alert = False
            if cached is None or not cached.get("is_in_error"):
                # State transition: OK -> ERROR
                should_alert = True
                logger.warning(f"Panel '{panel_name}' transitioned to ERROR state: {error_msg}")
            else:
                # Already in error; check cooldown
                last_time = cached.get("last_alert_at")
                if last_time:
                    secs = (now - last_time).total_seconds()
                    if secs >= cooldown_seconds or force_alert:
                        should_alert = True
                        logger.info(f"Cooldown elapsed ({secs:.0f}s >= {cooldown_seconds}s). Repeating Xray alert for '{panel_name}'.")

            if should_alert:
                if active_bot is None:
                    try:
                        from app.bot.main import create_bot
                        active_bot = create_bot()
                        should_close_bot = True
                    except Exception as bot_err:
                        logger.error(f"Cannot create Bot to send Xray alert: {bot_err}")

                if active_bot is not None:
                    time_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
                    alert_text = (
                        f"🚨 <b>هشدار خطای Xray در سرور و پنل!</b>\n\n"
                        f"🖥 <b>نام پنل:</b> <b>{clean_name}</b>\n"
                        f"🌐 <b>آدرس پنل:</b> <code>{clean_url}</code>{xray_ver}\n\n"
                        f"❌ <b>جزئیات خطا:</b>\n"
                        f"<code>{clean_err}</code>\n\n"
                        f"⏰ <b>زمان رخداد:</b> <code>{time_str}</code>\n\n"
                        f"⚠️ <i>توجه: ارتباط کاربران متصل به این پنل ممکن است با اختلال یا قطعی مواجه شده باشد. لطفاً وضعیت سرویس Xray را بررسی فرمایید.</i>"
                    )

                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🔄 تست و بررسی مجدد این پنل", callback_data=f"admin_recheck_panel:{panel.id}"),
                        ],
                        [
                            InlineKeyboardButton(text="🖥 مشاهده لیست پنل‌ها", callback_data="admin_view_panels"),
                            InlineKeyboardButton(text="⚙️ داشبورد مدیریت", callback_data="admin_refresh_dashboard"),
                        ],
                    ])

                    try:
                        await notify_all_admins(
                            bot=active_bot,
                            db_session=db_session,
                            text=alert_text,
                            reply_markup=kb,
                        )
                        alert_sent = True
                    except Exception as send_err:
                        logger.error(f"Failed to dispatch Xray error alert for panel '{panel_name}': {send_err}")

                _panel_alert_cache[cache_key] = {
                    "is_in_error": True,
                    "last_alert_at": now,
                    "last_error": error_msg,
                    "panel_name": panel_name,
                }

            # Update panel model
            panel.is_healthy = False
            panel.error_message = error_msg

        else:
            # Panel and Xray are healthy
            if cached and cached.get("is_in_error"):
                # State transition: ERROR -> OK (Recovery!)
                logger.info(f"Panel '{panel_name}' RECOVERED from previous error state.")
                if active_bot is None:
                    try:
                        from app.bot.main import create_bot
                        active_bot = create_bot()
                        should_close_bot = True
                    except Exception as bot_err:
                        logger.error(f"Cannot create Bot to send recovery alert: {bot_err}")

                if active_bot is not None:
                    time_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
                    recovery_text = (
                        f"✅ <b>برطرف شدن خطا و اتصال مجدد Xray</b>\n\n"
                        f"🖥 <b>نام پنل:</b> <b>{clean_name}</b>\n"
                        f"🌐 <b>آدرس پنل:</b> <code>{clean_url}</code>\n\n"
                        f"🟢 هسته Xray مجدداً فعال و پایدار شد و وضعیت پنل به حالت آنلاین (running) بازگشت.\n"
                        f"⏰ <b>زمان بازیابی:</b> <code>{time_str}</code>"
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🖥 مشاهده پنل‌ها", callback_data="admin_view_panels"),
                        ],
                    ])
                    try:
                        await notify_all_admins(
                            bot=active_bot,
                            db_session=db_session,
                            text=recovery_text,
                            reply_markup=kb,
                        )
                        alert_sent = True
                    except Exception as send_err:
                        logger.error(f"Failed to dispatch recovery alert for panel '{panel_name}': {send_err}")

                _panel_alert_cache[cache_key] = {
                    "is_in_error": False,
                    "last_alert_at": now,
                    "last_error": "",
                    "panel_name": panel_name,
                }
            elif cached is None:
                _panel_alert_cache[cache_key] = {
                    "is_in_error": False,
                    "last_alert_at": now,
                    "last_error": "",
                    "panel_name": panel_name,
                }

            panel.is_healthy = True
            panel.error_message = None
            panel.last_sync_at = now

        if should_close_bot and active_bot is not None:
            try:
                await active_bot.session.close()
            except Exception:
                pass

        return alert_sent

    @classmethod
    async def check_all_panels_and_alert(
        cls,
        db_session: AsyncSession,
        bot: Optional[Bot] = None,
    ) -> List[Dict[str, Any]]:
        """
        Iterate over all active panels, perform health & Xray status checks with consecutive retry verification,
        and dispatch notifications to admins if any panel has a persistent Xray error.
        """
        panels_res = await db_session.execute(select(Panel).where(Panel.is_active.is_(True)))
        panels = list(panels_res.scalars().all())

        results = []
        for panel in panels:
            is_healthy, err_msg, details = await cls.check_panel_health_with_retry(panel)
            alert_sent = await cls.record_panel_result(
                panel=panel,
                is_healthy=is_healthy,
                error_msg=err_msg,
                db_session=db_session,
                bot=bot,
                details=details,
            )
            results.append({
                "panel_id": str(panel.id),
                "panel_name": panel.name,
                "is_healthy": is_healthy,
                "error_message": err_msg,
                "alert_sent": alert_sent,
            })

        try:
            await db_session.commit()
        except Exception as commit_err:
            logger.error(f"Failed to commit panel health check updates: {commit_err}")
            await db_session.rollback()

        return results
