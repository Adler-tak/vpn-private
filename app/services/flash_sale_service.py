"""Flash Sale (تخفیف صاعقه‌ای تایمردار) Service for Mini App and Telegram Bot."""
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)


class FlashSaleService:
    """Manages timed flash sales, countdown timers, and dynamic discounts."""

    @classmethod
    def get_flash_sale_info(cls) -> Dict[str, Any]:
        """Get current flash sale status, title, discount percentage, and remaining seconds."""
        try:
            enabled_str = str(SettingsService.get_value("FLASH_SALE_ENABLED", "false")).lower()
            is_enabled = enabled_str in ("true", "1", "yes")

            discount_pct_str = str(SettingsService.get_value("FLASH_SALE_DISCOUNT_PERCENT", "30"))
            try:
                discount_pct = int(discount_pct_str)
            except ValueError:
                discount_pct = 30

            title = SettingsService.get_value("FLASH_SALE_TITLE", "⚡️ تخفیف ویژه صاعقه‌ای پایان هفته") or "⚡️ تخفیف ویژه صاعقه‌ای"
            ends_at_str = SettingsService.get_value("FLASH_SALE_ENDS_AT", "") or ""

            is_active = is_enabled
            remaining_seconds = 0

            if is_enabled and ends_at_str:
                try:
                    ends_at_dt = datetime.fromisoformat(ends_at_str.replace("Z", "+00:00"))
                    if ends_at_dt.tzinfo is None:
                        ends_at_dt = ends_at_dt.replace(tzinfo=timezone.utc)
                    now_utc = datetime.now(timezone.utc)
                    diff = (ends_at_dt - now_utc).total_seconds()
                    if diff <= 0:
                        is_active = False
                        remaining_seconds = 0
                    else:
                        remaining_seconds = int(diff)
                except Exception as parse_err:
                    logger.debug(f"Could not parse FLASH_SALE_ENDS_AT ({ends_at_str}): {parse_err}")
            elif is_enabled and not ends_at_str:
                # Active indefinitely until turned off
                remaining_seconds = 86400

            return {
                "is_active": is_active,
                "discount_percent": max(0, min(90, discount_pct)) if is_active else 0,
                "title": title,
                "ends_at": ends_at_str if is_active else None,
                "remaining_seconds": remaining_seconds,
            }
        except Exception as e:
            logger.error(f"Error fetching flash sale info: {e}")
            return {
                "is_active": False,
                "discount_percent": 0,
                "title": "تخفیف صاعقه‌ای",
                "ends_at": None,
                "remaining_seconds": 0,
            }

    @classmethod
    def calculate_discounted_price(cls, price_toman: int) -> int:
        """Calculate final price after flash sale discount."""
        if not price_toman or price_toman <= 0:
            return 0
        info = cls.get_flash_sale_info()
        if info["is_active"] and info["discount_percent"] > 0:
            discount = (price_toman * info["discount_percent"]) // 100
            return max(1000, price_toman - discount)
        return price_toman

    @classmethod
    async def configure_flash_sale(
        cls,
        is_enabled: bool,
        discount_percent: int = 30,
        title: str = "⚡️ تخفیف ویژه صاعقه‌ای",
        duration_hours: Optional[float] = None,
        ends_at_iso: Optional[str] = None,
        db_session: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Configure and persist flash sale settings."""
        ends_at_str = ""
        if is_enabled:
            if ends_at_iso:
                ends_at_str = ends_at_iso
            elif duration_hours and duration_hours > 0:
                from datetime import timedelta
                now_utc = datetime.now(timezone.utc)
                ends_at_dt = now_utc + timedelta(hours=duration_hours)
                ends_at_str = ends_at_dt.isoformat()

        await SettingsService.set_value("FLASH_SALE_ENABLED", "true" if is_enabled else "false", category="business", session=db_session)
        await SettingsService.set_value("FLASH_SALE_DISCOUNT_PERCENT", str(discount_percent), category="business", session=db_session)
        await SettingsService.set_value("FLASH_SALE_TITLE", title, category="business", session=db_session)
        await SettingsService.set_value("FLASH_SALE_ENDS_AT", ends_at_str, category="business", session=db_session)

        return cls.get_flash_sale_info()
