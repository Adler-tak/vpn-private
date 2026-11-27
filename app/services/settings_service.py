"""
Clean, organized System Settings Service (3-to-97 Rule).
Manages dynamic settings directly in the database (`system_settings` table),
providing in-memory caching and clean categorization.
"""
import os
import json
import inspect
import logging
from typing import Any, Optional, Dict, List
from sqlalchemy import select
from app.database.session import async_session_factory, sync_session_factory
from app.database.models.setting import SystemSetting

logger = logging.getLogger(__name__)

# Default structured settings definitions
DEFAULT_SYSTEM_SETTINGS: List[Dict[str, str]] = [
    # --- Payment Settings ---
    {
        "key": "CARD_NUMBER",
        "value": "6037997100000000",
        "category": "payment",
        "description": "شماره کارت ۱۶ رقمی جهت واریز کارت به کارت",
    },
    {
        "key": "CARD_HOLDER",
        "value": "مدیر سیستم",
        "category": "payment",
        "description": "نام دارنده کارت بانکی",
    },
    {
        "key": "CARD_BANK_NAME",
        "value": "بانک ملی ایران",
        "category": "payment",
        "description": "نام بانک صادرکننده کارت",
    },
    {
        "key": "CARD_IBAN",
        "value": "IR000000000000000000000000",
        "category": "payment",
        "description": "شماره شبا حساب بانکی",
    },
    {
        "key": "CARD_TO_CARD_ENABLED",
        "value": "false",
        "category": "payment",
        "description": "فعال یا غیرفعال بودن درگاه کارت به کارت",
    },
    {
        "key": "ZARINPAL_MERCHANT_ID",
        "value": "00000000-0000-0000-0000-000000000000",
        "category": "payment",
        "description": "مرچنت آی‌دی درگاه پرداخت آنلاین زرین‌پال",
    },
    {
        "key": "ZARINPAL_ENABLED",
        "value": "false",
        "category": "payment",
        "description": "فعال یا غیرفعال بودن درگاه زرین‌پال",
    },
    {
        "key": "MIN_WALLET_TOPUP_TOMAN",
        "value": "10000",
        "category": "payment",
        "description": "حداقل مبلغ شارژ کیف پول (تومان)",
    },
    {
        "key": "WALLET_ENABLED",
        "value": "true",
        "category": "payment",
        "description": "فعال یا غیرفعال بودن سیستم کیف پول",
    },

    # --- Crypto Settings ---
    {
        "key": "CRYPTO_ENABLED",
        "value": "false",
        "category": "crypto",
        "description": "فعال بودن پرداخت ارزی با تتر و ارز دیجیتال",
    },
    {
        "key": "CRYPTO_TRC20_ENABLED",
        "value": "true",
        "category": "crypto",
        "description": "فعال بودن شبکه ترون (USDT-TRC20)",
    },
    {
        "key": "CRYPTO_WALLET_TRC20",
        "value": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
        "category": "crypto",
        "description": "آدرس ولت دریافت تتر شبکه TRC20",
    },
    {
        "key": "CRYPTO_WALLET_USDT",
        "value": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
        "category": "crypto",
        "description": "آدرس ولت تتر پیش‌فرض",
    },
    {
        "key": "CRYPTO_BEP20_ENABLED",
        "value": "false",
        "category": "crypto",
        "description": "فعال بودن شبکه بایننس اسمارت چین (USDT-BEP20)",
    },
    {
        "key": "CRYPTO_WALLET_BEP20",
        "value": "0x587EF4ea392c6947688533fb0550Bae94B8f0E8a",
        "category": "crypto",
        "description": "آدرس ولت دریافت تتر شبکه BEP20",
    },
    {
        "key": "USDT_TOMAN_RATE",
        "value": "70000",
        "category": "crypto",
        "description": "نرخ تبدیل هر تتر به تومان",
    },
    {
        "key": "USDT_TRC20_RATE",
        "value": "70000",
        "category": "crypto",
        "description": "نرخ تتر TRC20 به تومان",
    },

    # --- Telegram Bot Settings ---
    {
        "key": "CHANNEL_USERNAME",
        "value": "",
        "category": "telegram",
        "description": "آیدی کانال اطلاع‌رسانی ربات (بدون @)",
    },
    {
        "key": "SUPPORT_USERNAME",
        "value": "",
        "category": "telegram",
        "description": "آیدی پشتیبانی تلگرام (بدون @)",
    },
    {
        "key": "MANDATORY_CHANNEL_ENABLED",
        "value": "false",
        "category": "telegram",
        "description": "فعال بودن قفل عضویت اجباری کانال تلگرام",
    },
    {
        "key": "BOT_USERNAME",
        "value": "VPNPrivateSell_Bot",
        "category": "telegram",
        "description": "نام کاربری ربات تلگرام",
    },
    {
        "key": "CLIENT_EMAIL_SUFFIX",
        "value": "",
        "category": "telegram",
        "description": "پسوند ایمیل کاربران در کانفیگ‌ها (خالی = @آیدی_ربات)",
    },
    {
        "key": "CONFIG_TITLE_PREFIX",
        "value": "VPN-Private",
        "category": "telegram",
        "description": "پیشوند عنوان و ریمارک نمایشی کانفیگ‌ها",
    },

    # --- Trial Subscription Settings ---
    {
        "key": "TRIAL_ENABLED",
        "value": "true",
        "category": "trial",
        "description": "امکان دریافت اکانت تست رایگان برای کاربران",
    },
    {
        "key": "TRIAL_TRAFFIC_MB",
        "value": "1024",
        "category": "trial",
        "description": "حجم اکانت تست رایگان به مگابایت",
    },
    {
        "key": "TRIAL_DURATION_DAYS",
        "value": "1",
        "category": "trial",
        "description": "مدت زمان اعتبار اکانت تست به روز",
    },
    {
        "key": "TRIAL_DURATION_HOURS",
        "value": "24",
        "category": "trial",
        "description": "مدت زمان اعتبار اکانت تست به ساعت",
    },
    {
        "key": "TRIAL_MAX_COUNT",
        "value": "1",
        "category": "trial",
        "description": "حداکثر دفعات دریافت تست برای هر کاربر",
    },
    {
        "key": "TRIAL_INBOUND_ID",
        "value": "",
        "category": "trial",
        "description": "شناسه اینباند اختصاصی اکانت تست رایگان",
    },

    # --- Business & Affiliate Settings ---
    {
        "key": "REFERRAL_COMMISSION_PERCENT",
        "value": "10",
        "category": "business",
        "description": "درصد کمیسیون و پورسانت بازاریابی رفرال",
    },
    {
        "key": "FLASH_SALE_ENABLED",
        "value": "false",
        "category": "business",
        "description": "فعال یا غیرفعال بودن تخفیف صاعقه‌ای تایمردار",
    },
    {
        "key": "FLASH_SALE_DISCOUNT_PERCENT",
        "value": "30",
        "category": "business",
        "description": "درصد تخفیف صاعقه‌ای",
    },
    {
        "key": "FLASH_SALE_TITLE",
        "value": "⚡️ تخفیف ویژه صاعقه‌ای پایان هفته",
        "category": "business",
        "description": "عنوان تخفیف صاعقه‌ای در مینی‌اپ و بات",
    },
    {
        "key": "FLASH_SALE_ENDS_AT",
        "value": "",
        "category": "business",
        "description": "زمان انقضای تخفیف صاعقه‌ای به فرمت ISO",
    },
    {
        "key": "WINBACK_ENABLED",
        "value": "true",
        "category": "business",
        "description": "فعال یا غیرفعال بودن اتوماسیون بازگرداندن مشتریان رفته",
    },
    {
        "key": "WINBACK_DISCOUNT_PERCENT",
        "value": "35",
        "category": "business",
        "description": "درصد تخفیف هدیه برای بازگرداندن مشتریان رفته",
    },
    {
        "key": "WINBACK_COUPON_CODE",
        "value": "COMEBACK35",
        "category": "business",
        "description": "کد تخفیف هدیه ارسالی به مشتریان رفته",
    },
    {
        "key": "WINBACK_COOLDOWN_DAYS",
        "value": "30",
        "category": "business",
        "description": "فاصله زمانی بین ارسال‌های مجدد به یک کاربر به روز",
    },

    # --- Security & Web Admin Settings ---
    {
        "key": "ADMIN_WEB_USERNAME",
        "value": "admin",
        "category": "security",
        "description": "نام کاربری ورود به پنل وب ادمین",
    },
    {
        "key": "ADMIN_WEB_PASSWORD",
        "value": "admin",
        "category": "security",
        "description": "رمز عبور ورود به پنل وب ادمین",
    },
    {
        "key": "ADMIN_WEB_PASSWORD_HASH",
        "value": "",
        "category": "security",
        "description": "هش رمز عبور bcrypt پنل مدیریت",
    },
    {
        "key": "ADMIN_2FA_ENABLED",
        "value": "false",
        "category": "security",
        "description": "فعال بودن احراز هویت دوعاملی 2FA",
    },
    {
        "key": "ADMIN_2FA_TELEGRAM_ID",
        "value": "0",
        "category": "security",
        "description": "شناسه عددی تلگرام ادمین برای دریافت کد تایید ورود",
    },
    {
        "key": "ADMIN_TOTP_SECRET",
        "value": "",
        "category": "security",
        "description": "کلید امنیتی Base32 گوگل اتنتیکیتور",
    },

    # --- Panel & Xray Health Monitoring Settings ---
    {
        "key": "PANEL_HEALTH_CHECK_INTERVAL",
        "value": "60",
        "category": "monitoring",
        "description": "بازه زمانی بررسی دوره‌ای سلامت پنل‌ها (به ثانیه)",
    },
    {
        "key": "PANEL_HEALTH_RETRY_COUNT",
        "value": "5",
        "category": "monitoring",
        "description": "تعداد دفعات تست مجدد متوالی قبل از ارسال هشدار خطا به تلگرام",
    },
    {
        "key": "PANEL_HEALTH_RETRY_INTERVAL",
        "value": "5",
        "category": "monitoring",
        "description": "فاصله زمانی بین هر تلاش مجدد (به ثانیه)",
    },
    {
        "key": "PANEL_HEALTH_ALERT_COOLDOWN",
        "value": "30",
        "category": "monitoring",
        "description": "حداقل فاصله زمانی بین ارسال هشدارهای تکراری خطا (به دقیقه)",
    },
]

# Fast in-memory cache for synchronous reads with cross-container mtime auto-refresh
_settings_cache: Dict[str, Any] = {}
_settings_initialized: bool = False
_last_db_sync_check: float = 0.0
_last_db_mtime: float = 0.0
_last_json_mtime: float = 0.0


class _AwaitableNone:
    _is_none = True

    def __await__(self):
        async def _coro():
            return None
        return _coro().__await__()

    def __bool__(self):
        return False

    def __eq__(self, other):
        return other is None or isinstance(other, _AwaitableNone)

    def __str__(self):
        return ""

    def __repr__(self):
        return "None"


class _AwaitableStr(str):
    def __await__(self):
        async def _coro():
            return str(self)
        return _coro().__await__()


class SettingsService:
    @classmethod
    def _refresh_cache_if_stale(cls) -> None:
        """
        Cross-container cache synchronization (vpn-api <-> vpn-bot):
        Checks mtime of system_settings.json and vpn_private.db at most once per second.
        When vpn-api updates settings in Web Admin, vpn-bot immediately reloads fresh DB values.
        """
        global _settings_cache, _last_db_sync_check, _last_db_mtime, _last_json_mtime
        import time
        now = time.monotonic()
        if (now - _last_db_sync_check) < 1.0 and _settings_cache:
            return
        _last_db_sync_check = now

        try:
            from app.core.config import PROJECT_ROOT, SETTINGS_JSON_PATH
            db_path = PROJECT_ROOT / "vpn_private.db"
            db_mtime = os.path.getmtime(db_path) if db_path.exists() else 0.0
            json_mtime = os.path.getmtime(SETTINGS_JSON_PATH) if SETTINGS_JSON_PATH.exists() else 0.0

            if db_mtime != _last_db_mtime or json_mtime != _last_json_mtime:
                with sync_session_factory() as session:
                    res = session.execute(select(SystemSetting))
                    rows = res.scalars().all()
                    if rows:
                        for row in rows:
                            _settings_cache[row.key.upper()] = row.value
                _last_db_mtime = db_mtime
                _last_json_mtime = json_mtime
        except Exception:
            pass

    @classmethod
    def get_cache(cls) -> Dict[str, Any]:
        global _settings_cache
        cls._refresh_cache_if_stale()
        return _settings_cache

    @classmethod
    def get_value(cls, key: str, default: Any = None) -> Any:
        """Read setting from in-memory cache (with cross-container mtime sync).
        Returns an awaitable-compatible string/value so callers can invoke either synchronously
        or via `await SettingsService.get_value(...)`.
        """
        if not key or not isinstance(key, str):
            res = default
        else:
            cls._refresh_cache_if_stale()
            k = key.strip().upper()
            if k in _settings_cache:
                val = _settings_cache[k]
                res = val if val is not None else default
            else:
                res = default

        if res is None:
            return _AwaitableNone()
        if isinstance(res, str) and not isinstance(res, _AwaitableStr):
            return _AwaitableStr(res)
        return res

    @classmethod
    async def get_value_async(cls, key: str, default: Any = None, session: Optional[Any] = None) -> Any:
        """Asynchronous get_value for explicit async calls."""
        res = cls.get_value(key, default)
        if inspect.isawaitable(res):
            return await res
        return res

    @classmethod
    def get_int(cls, key: str, default: int = 0) -> int:
        """Read setting as integer with safe fallback on corruption."""
        val = cls.get_value(key)
        if val is None or val == "":
            return default
        try:
            if isinstance(val, (int, float)):
                return int(val)
            cleaned = str(val).strip()
            if "." in cleaned:
                return int(float(cleaned))
            return int(cleaned)
        except (ValueError, TypeError):
            logger.warning(f"Corrupted integer setting '{key}': {val!r}, falling back to {default}")
            return default

    @classmethod
    def get_bool(cls, key: str, default: bool = False) -> bool:
        """Read setting as boolean with safe fallback on corruption."""
        val = cls.get_value(key)
        if val is None or val == "":
            return default
        if isinstance(val, bool):
            return val
        s = str(val).strip().lower()
        if s in ("true", "1", "yes", "on", "enable", "enabled", "y", "t"):
            return True
        if s in ("false", "0", "no", "off", "disable", "disabled", "n", "f"):
            return False
        logger.warning(f"Corrupted boolean setting '{key}': {val!r}, falling back to {default}")
        return default

    @classmethod
    def get_float(cls, key: str, default: float = 0.0) -> float:
        """Read setting as float with safe fallback on corruption."""
        val = cls.get_value(key)
        if val is None or val == "":
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            logger.warning(f"Corrupted float setting '{key}': {val!r}, falling back to {default}")
            return default

    @classmethod
    def get_str(cls, key: str, default: str = "") -> str:
        """Read setting as string with safe fallback."""
        val = cls.get_value(key)
        if val is None:
            return default
        return str(val)

    @classmethod
    async def initialize_and_seed(cls) -> None:
        """
        Seed default settings into the database table if not already present,
        and load all existing settings into the in-memory cache.
        """
        global _settings_cache, _settings_initialized
        try:
            from app.core.config import _load_system_settings_json
            import os
            json_overrides = _load_system_settings_json()
        except Exception:
            json_overrides = {}

        try:
            async with async_session_factory() as session:
                # 1. Fetch all existing settings from DB
                res = await session.execute(select(SystemSetting))
                existing_rows = res.scalars().all()
                existing_keys = {row.key.upper(): row for row in existing_rows}

                # 2. Seed missing defaults with installer/env overrides
                to_add = []
                for default_item in DEFAULT_SYSTEM_SETTINGS:
                    k = default_item["key"].upper()
                    if k not in existing_keys:
                        init_val = str(default_item["value"])
                        if k in json_overrides and str(json_overrides[k]).strip():
                            init_val = str(json_overrides[k]).strip()
                        elif os.environ.get(k) and str(os.environ[k]).strip():
                            init_val = str(os.environ[k]).strip()

                        new_setting = SystemSetting(
                            key=k,
                            value=init_val,
                            category=default_item["category"],
                            description=default_item["description"],
                        )
                        to_add.append(new_setting)
                        existing_keys[k] = new_setting
                    else:
                        # Synchronize installer overrides if DB still holds default 'admin'
                        if k in ("ADMIN_WEB_USERNAME", "ADMIN_WEB_PASSWORD", "ADMIN_WEB_PASSWORD_HASH"):
                            custom_val = json_overrides.get(k) or os.environ.get(k)
                            if custom_val and str(custom_val).strip() and str(custom_val).strip() != "admin":
                                row = existing_keys[k]
                                if row.value == "admin":
                                    row.value = str(custom_val).strip()
                                    session.add(row)
                                    logger.info(f"Synchronized {k} from installer overrides into DB.")

                if to_add:
                    session.add_all(to_add)
                await session.commit()
                if to_add:
                    logger.info(f"Seeded {len(to_add)} missing system settings into database.")

                # 3. Populate in-memory cache
                for k, row in existing_keys.items():
                    _settings_cache[k] = row.value

                _settings_initialized = True
                logger.info(f"System settings cache loaded ({len(_settings_cache)} settings).")
        except Exception as e:
            logger.warning(f"Could not initialize system settings from DB: {e}")
            # Fallback to defaults in cache
            if not _settings_cache:
                for item in DEFAULT_SYSTEM_SETTINGS:
                    _settings_cache[item["key"].upper()] = item["value"]

    @classmethod
    def initialize_sync(cls) -> None:
        """Synchronous version for Celery, CLI scripts, or offline runners."""
        global _settings_cache, _settings_initialized
        try:
            from app.core.config import _load_system_settings_json
            import os
            json_overrides = _load_system_settings_json()
        except Exception:
            json_overrides = {}

        try:
            with sync_session_factory() as session:
                res = session.execute(select(SystemSetting))
                existing_rows = res.scalars().all()
                existing_keys = {row.key.upper(): row for row in existing_rows}

                to_add = []
                for default_item in DEFAULT_SYSTEM_SETTINGS:
                    k = default_item["key"].upper()
                    if k not in existing_keys:
                        init_val = str(default_item["value"])
                        if k in json_overrides and str(json_overrides[k]).strip():
                            init_val = str(json_overrides[k]).strip()
                        elif os.environ.get(k) and str(os.environ[k]).strip():
                            init_val = str(os.environ[k]).strip()

                        new_setting = SystemSetting(
                            key=k,
                            value=init_val,
                            category=default_item["category"],
                            description=default_item["description"],
                        )
                        to_add.append(new_setting)
                        existing_keys[k] = new_setting
                    else:
                        if k in ("ADMIN_WEB_USERNAME", "ADMIN_WEB_PASSWORD", "ADMIN_WEB_PASSWORD_HASH"):
                            custom_val = json_overrides.get(k) or os.environ.get(k)
                            if custom_val and str(custom_val).strip() and str(custom_val).strip() != "admin":
                                row = existing_keys[k]
                                if row.value == "admin":
                                    row.value = str(custom_val).strip()
                                    session.add(row)

                if to_add:
                    session.add_all(to_add)
                session.commit()

                for k, row in existing_keys.items():
                    _settings_cache[k] = row.value

                _settings_initialized = True
        except Exception as e:
            logger.warning(f"Sync settings init warning: {e}")
            if not _settings_cache:
                for item in DEFAULT_SYSTEM_SETTINGS:
                    _settings_cache[item["key"].upper()] = item["value"]

    @classmethod
    async def set_value(
        cls,
        key: str,
        value: Any,
        category: Optional[str] = None,
        description: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        """
        Save setting to database table and immediately update the in-memory cache.
        Persists to database first to prevent cache desynchronization on transaction failure.
        Does NOT touch .env!
        """
        global _settings_cache
        if not key or not isinstance(key, str):
            return
        k = key.strip().upper()
        if isinstance(value, bool):
            val_str = "true" if value else "false"
        elif value is None:
            val_str = ""
        else:
            val_str = str(value)

        async def _persist(s: Any, should_commit: bool):
            setting = await s.get(SystemSetting, k)
            if setting:
                setting.value = val_str
                if category:
                    setting.category = category
                if description:
                    setting.description = description
            else:
                cat = category or "general"
                desc = description
                for item in DEFAULT_SYSTEM_SETTINGS:
                    if item["key"].upper() == k:
                        cat = item["category"]
                        desc = desc or item["description"]
                        break
                setting = SystemSetting(
                    key=k,
                    value=val_str,
                    category=cat,
                    description=desc,
                )
                s.add(setting)
            if should_commit:
                await s.commit()

        try:
            if session is not None:
                await _persist(session, should_commit=False)
            else:
                async with async_session_factory() as s:
                    await _persist(s, should_commit=True)
            _settings_cache[k] = val_str
            try:
                from app.core.config import save_system_settings_json
                save_system_settings_json(k, val_str)
            except Exception:
                pass
            logger.info(f"System setting '{k}' saved to database and cache.")
        except Exception as e:
            logger.error(f"Error persisting system setting '{k}' to database: {e}")
            raise

    @classmethod
    def set_value_sync(
        cls,
        key: str,
        value: Any,
        category: Optional[str] = None,
        description: Optional[str] = None,
        session: Optional[Any] = None,
    ) -> None:
        """Synchronous version of set_value with atomic cache synchronization."""
        global _settings_cache
        if not key or not isinstance(key, str):
            return
        k = key.strip().upper()
        if isinstance(value, bool):
            val_str = "true" if value else "false"
        elif value is None:
            val_str = ""
        else:
            val_str = str(value)

        def _persist(s: Any, should_commit: bool):
            setting = s.get(SystemSetting, k)
            if setting:
                setting.value = val_str
                if category:
                    setting.category = category
                if description:
                    setting.description = description
            else:
                cat = category or "general"
                desc = description
                for item in DEFAULT_SYSTEM_SETTINGS:
                    if item["key"].upper() == k:
                        cat = item["category"]
                        desc = desc or item["description"]
                        break
                setting = SystemSetting(
                    key=k,
                    value=val_str,
                    category=cat,
                    description=desc,
                )
                s.add(setting)
            if should_commit:
                s.commit()

        try:
            if session is not None:
                _persist(session, should_commit=False)
            else:
                with sync_session_factory() as s:
                    _persist(s, should_commit=True)
            _settings_cache[k] = val_str
            try:
                from app.core.config import save_system_settings_json
                save_system_settings_json(k, val_str)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error persisting system setting '{k}' synchronously: {e}")
            raise

    @classmethod
    async def get_all(cls) -> List[Dict[str, Any]]:
        """Return all system settings grouped and formatted cleanly."""
        try:
            async with async_session_factory() as session:
                res = await session.execute(select(SystemSetting).order_by(SystemSetting.category, SystemSetting.key))
                rows = res.scalars().all()
                return [
                    {
                        "key": r.key,
                        "value": r.value,
                        "category": r.category,
                        "description": r.description,
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.warning(f"Error fetching all system settings: {e}")
            return []

    @classmethod
    async def get_effective_email_suffix(cls, session=None) -> str:
        """
        If CLIENT_EMAIL_SUFFIX is set and non-empty, return it (ensure leading @).
        Else get effective bot username (using get_effective_bot_username(session)) and return @ + username.
        Else fallback to @vpnprivate.
        """
        try:
            val = cls.get_value("CLIENT_EMAIL_SUFFIX")
            if inspect.isawaitable(val):
                val = await val
            if val and str(val).strip():
                clean = str(val).strip()
                return clean if clean.startswith("@") else f"@{clean}"
        except Exception as e:
            logger.debug(f"Could not load CLIENT_EMAIL_SUFFIX: {e}")

        try:
            bot_user = await get_effective_bot_username(session)
            if bot_user and str(bot_user).strip():
                clean_bot = str(bot_user).strip().lstrip("@")
                if clean_bot:
                    return f"@{clean_bot}"
        except Exception as e:
            logger.debug(f"Could not load bot username for email suffix: {e}")

        return "@vpnprivate"

    @classmethod
    async def get_effective_config_title_prefix(cls, session=None) -> str:
        """
        If CONFIG_TITLE_PREFIX is set and non-empty, return it.
        Else fallback to 'VPN-Private'.
        """
        try:
            val = cls.get_value("CONFIG_TITLE_PREFIX")
            if inspect.isawaitable(val):
                val = await val
            if val and str(val).strip():
                return str(val).strip()
        except Exception as e:
            logger.debug(f"Could not load CONFIG_TITLE_PREFIX: {e}")

        return "VPN-Private"


async def get_effective_bot_username(session: Optional[Any] = None) -> str:
    """
    Resolve the active bot username dynamically.
    Priority:
    1. Active TelegramBot in DB table (is_active == True)
    2. SettingsService (SystemSetting table / cache) 'BOT_USERNAME'
    3. get_live_env("BOT_USERNAME")
    4. settings.BOT_USERNAME
    5. Fallback: "VPNPrivateSell_Bot"
    """
    # 1. Active TelegramBot from DB
    try:
        from sqlalchemy import select
        from app.database.models.bot import TelegramBot
        if session:
            stmt = select(TelegramBot.username).where(TelegramBot.is_active == True).limit(1)
            active_u = (await session.execute(stmt)).scalar_one_or_none()
            if active_u and str(active_u).strip():
                return str(active_u).lstrip("@").strip()
        else:
            async with async_session_factory() as s:
                stmt = select(TelegramBot.username).where(TelegramBot.is_active == True).limit(1)
                active_u = (await s.execute(stmt)).scalar_one_or_none()
                if active_u and str(active_u).strip():
                    return str(active_u).lstrip("@").strip()
    except Exception as e:
        logger.debug(f"Could not load active TelegramBot from DB: {e}")

    # 2. SystemSetting in DB / cache
    try:
        cached_u = SettingsService.get_str("BOT_USERNAME", "")
        if cached_u and cached_u.strip() and cached_u != "VPNPrivateBot":
            return cached_u.lstrip("@").strip()
    except Exception:
        pass

    # 3. get_live_env
    try:
        env_u = get_live_env("BOT_USERNAME", "")
        if env_u and env_u.strip() and env_u != "VPNPrivateBot":
            return env_u.lstrip("@").strip()
    except Exception:
        pass

    # 4. settings.BOT_USERNAME
    try:
        from app.core.config import settings
        cfg_u = getattr(settings, "BOT_USERNAME", "")
        if cfg_u and cfg_u.strip() and cfg_u != "VPNPrivateBot":
            return cfg_u.lstrip("@").strip()
    except Exception:
        pass

    return "VPNPrivateSell_Bot"


def get_effective_bot_username_sync() -> str:
    """Synchronous resolution of active bot username."""
    # 1. Try sync DB query for active TelegramBot
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        from app.database.models.bot import TelegramBot
        from app.core.config import settings

        sync_url = getattr(settings, "DATABASE_URL_SYNC", "")
        if "sqlite" in settings.DATABASE_URL and "sqlite" not in sync_url:
            sync_url = settings.DATABASE_URL.replace("sqlite+aiosqlite", "sqlite")
        elif "postgresql" in settings.DATABASE_URL and "postgresql" not in sync_url:
            sync_url = settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql")

        if sync_url:
            if "sqlite" in sync_url:
                engine = create_engine(sync_url, connect_args={"check_same_thread": False})
            else:
                engine = create_engine(sync_url, pool_pre_ping=True)
            with Session(engine) as s:
                stmt = select(TelegramBot.username).where(TelegramBot.is_active == True).limit(1)
                active_u = s.execute(stmt).scalar_one_or_none()
                if active_u and str(active_u).strip():
                    return str(active_u).lstrip("@").strip()
    except Exception:
        pass

    # 2. SystemSetting cache
    try:
        cached_u = SettingsService.get_str("BOT_USERNAME", "")
        if cached_u and cached_u.strip() and cached_u != "VPNPrivateBot":
            return cached_u.lstrip("@").strip()
    except Exception:
        pass

    # 3. get_live_env
    try:
        env_u = get_live_env("BOT_USERNAME", "")
        if env_u and env_u.strip() and env_u != "VPNPrivateBot":
            return env_u.lstrip("@").strip()
    except Exception:
        pass

    # 4. settings.BOT_USERNAME
    try:
        from app.core.config import settings
        cfg_u = getattr(settings, "BOT_USERNAME", "")
        if cfg_u and cfg_u.strip() and cfg_u != "VPNPrivateBot":
            return cfg_u.lstrip("@").strip()
    except Exception:
        pass

    return "VPNPrivateSell_Bot"


get_effective_email_suffix = SettingsService.get_effective_email_suffix
get_effective_config_title_prefix = SettingsService.get_effective_config_title_prefix

