"""Configuration module using Pydantic Settings with Live Inter-Process Synchronization."""
import os
import re
import json
import logging
from pathlib import Path
from typing import List, Optional, Any
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
SETTINGS_JSON_PATH = DATA_DIR / "system_settings.json"

_env_mtime: float = 0.0
_env_cache: dict[str, str] = {}

_settings_json_mtime: float = 0.0
_settings_json_cache: dict[str, Any] = {}


def _load_system_settings_json() -> dict[str, Any]:
    """Read dynamic 97% settings from persistent json store using mtime sync."""
    global _settings_json_mtime, _settings_json_cache
    if SETTINGS_JSON_PATH.exists():
        try:
            curr_mtime = os.path.getmtime(SETTINGS_JSON_PATH)
            if curr_mtime != _settings_json_mtime:
                raw = SETTINGS_JSON_PATH.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    _settings_json_cache = {str(k).strip().upper(): v for k, v in data.items()}
                    _settings_json_mtime = curr_mtime
        except Exception as e:
            logger.warning(f"Error reading system_settings.json: {e}")
    return _settings_json_cache


def save_system_settings_json(key: str, value: Any):
    """Write dynamic setting to persistent json store with atomic write."""
    global _settings_json_mtime, _settings_json_cache
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        current = {}
        if SETTINGS_JSON_PATH.exists():
            try:
                current = json.loads(SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
            except Exception:
                current = {}
        current[key.strip().upper()] = value
        tmp_path = SETTINGS_JSON_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(SETTINGS_JSON_PATH)
        _settings_json_cache[key.strip().upper()] = value
        _settings_json_mtime = os.path.getmtime(SETTINGS_JSON_PATH)
    except Exception as e:
        logger.warning(f"Error saving system_settings.json: {e}")


def get_live_env(key: str, default: Any = None) -> Any:
    """
    3-to-97 Rule Implementation:
    1. Check dynamic database-backed settings cache (SettingsService) with cross-process mtime sync.
    2. Fallback to local persistent json store (system_settings.json).
    3. Fallback to .env file if key not found in dynamic store.
    4. Return default fallback value.
    """
    if not key or not isinstance(key, str):
        return default
    k_upper = key.strip().upper()

    # 1. Sync persistent json store first so cross-container updates (vpn-api -> vpn-bot) propagate immediately
    json_settings = _load_system_settings_json()

    # 2. Database-backed settings cache check (auto-reloads when DB or JSON file mtime changes)
    try:
        from app.services.settings_service import SettingsService
        cached_val = SettingsService.get_value(k_upper)
        if cached_val is not None and not getattr(cached_val, "_is_none", False):
            return cached_val
    except Exception:
        pass

    if k_upper in json_settings:
        return json_settings[k_upper]

    # 3. .env file check with live mtime reload
    global _env_mtime, _env_cache
    if ENV_PATH.exists():
        try:
            curr_mtime = os.path.getmtime(ENV_PATH)
            if curr_mtime != _env_mtime:
                lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
                new_cache = {}
                for l in lines:
                    l = l.strip()
                    if l and not l.startswith("#") and "=" in l:
                        k, v = l.split("=", 1)
                        new_cache[k.strip().upper()] = v.strip().strip("\"'")
                _env_cache = new_cache
                _env_mtime = curr_mtime
        except Exception:
            pass

    return _env_cache.get(k_upper, default)


def update_env_variable(key: str, value: Any):
    """
    3-to-97 Rule Implementation:
    Atomically updates database table (system_settings),
    updates local runtime cache, and updates in-memory process cache.
    Does NOT pollute .env with dynamic settings.
    """
    global _env_mtime, _env_cache
    if not key or not isinstance(key, str):
        return
    k_upper = key.strip().upper()
    if isinstance(value, bool):
        val_str = "true" if value else "false"
    elif value is None:
        val_str = ""
    else:
        val_str = str(value)

    # 1. Persist to database table via SettingsService
    try:
        from app.services.settings_service import SettingsService
        SettingsService.set_value_sync(k_upper, val_str)
    except Exception as e:
        logger.debug(f"Sync settings service update notice: {e}")

    # 2. Persist to dynamic store (data/system_settings.json) as offline/fast fallback
    save_system_settings_json(k_upper, value)

    # 3. Update in-memory cache
    _env_cache[k_upper] = val_str
    logger.info(f"Synchronized dynamic setting {key}={val_str}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # General
    PROJECT_NAME: str = "VPN Private"
    DEBUG: bool = False

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug(cls, v: Any) -> bool:
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes", "on", "debug")
        return bool(v)

    ENVIRONMENT: str = "production"
    SECRET_KEY: str = "vpn-private-super-secure-production-secret-change-me-32b"
    ENCRYPTION_KEY: str = "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcw==" # Fernet base64 key
    API_V1_STR: str = "/api/v1"
    SERVER_HOST: str = "http://localhost:8000"
    WEBAPP_URL: Optional[str] = None

    # Telegram Bot
    BOT_TOKEN: str = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
    TELEGRAM_PROXY: Optional[str] = None
    TELEGRAM_PROXY_ENABLED: bool = False
    ADMIN_TELEGRAM_IDS: Any = Field(default_factory=list)

    @field_validator("ADMIN_TELEGRAM_IDS", mode="before")
    @classmethod
    def parse_admin_telegram_ids(cls, v: Any) -> List[int]:
        if isinstance(v, (list, tuple, set)):
            return [int(x) for x in v if str(x).strip().isdigit() or isinstance(x, int)]
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                import json
                try:
                    parsed = json.loads(v)
                    return [int(x) for x in parsed]
                except Exception:
                    pass
            import re
            parts = re.split(r"[,;\s]+", v)
            return [int(p) for p in parts if p.isdigit()]
        if isinstance(v, int):
            return [v]
        return []

    BOT_USERNAME: str = "VPNPrivateSell_Bot"

    # Database & Cache
    DATABASE_URL: str = "postgresql+asyncpg://vpn_user:vpn_secret_password@localhost:5432/vpn_private"
    DATABASE_URL_SYNC: str = "postgresql://vpn_user:vpn_secret_password@localhost:5432/vpn_private"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # Payments: Zarinpal
    ZARINPAL_SANDBOX: bool = True
    ZARINPAL_CALLBACK_URL: str = "http://localhost:8000/api/v1/payments/zarinpal/callback"

    # Payments: Stripe
    STRIPE_SUCCESS_URL: str = "http://localhost:8000/api/v1/payments/stripe/callback?session_id={CHECKOUT_SESSION_ID}"
    STRIPE_CANCEL_URL: str = "http://localhost:8000/api/v1/payments/stripe/cancel"

    # Payments: Perfect Money
    PERFECT_MONEY_ENABLED: bool = False
    PERFECT_MONEY_ACCOUNT: str = "U12345678"
    PERFECT_MONEY_PASSPHRASE: str = ""
    PERFECT_MONEY_USD_RATE: int = 70000
    PERFECT_MONEY_CALLBACK_URL: str = "http://localhost:8000/api/v1/payments/perfectmoney/callback"
    PERFECT_MONEY_STATUS_URL: str = "http://localhost:8000/api/v1/payments/perfectmoney/status"

    # Business Rules Defaults
    RESELLER_DISCOUNT_PERCENT: int = 30
    LOG_CHANNEL_ID: str | None = None

    # PIA WireGuard Auto Updater Credentials
    PIA_USERNAME: str = ""
    PIA_PASSWORD: str = ""

    # Windscribe WireGuard Auto Updater Credentials
    WINDSCRIBE_USERNAME: str = ""
    WINDSCRIBE_PASSWORD: str = ""
    WINDSCRIBE_SESSION_TOKEN: str = ""
    WINDSCRIBE_PRIVATE_KEY: str = ""
    WINDSCRIBE_PUBLIC_KEY: str = ""
    WINDSCRIBE_PRESHARED_KEY: str = ""
    WINDSCRIBE_ADDRESS: str = "100.64.0.2/32"

    # =========================================================
    # DYNAMIC RUNTIME-SYNCED PROPERTIES ACROSS BOT & WEB PANEL
    # =========================================================
    @property
    def CARD_NUMBER(self) -> str:
        return str(get_live_env("CARD_NUMBER", "6037997100000000"))

    @CARD_NUMBER.setter
    def CARD_NUMBER(self, val: str):
        update_env_variable("CARD_NUMBER", val)

    @property
    def CARD_HOLDER(self) -> str:
        return str(get_live_env("CARD_HOLDER", "مدیر سیستم"))

    @CARD_HOLDER.setter
    def CARD_HOLDER(self, val: str):
        update_env_variable("CARD_HOLDER", val)

    @property
    def CARD_BANK_NAME(self) -> str:
        return str(get_live_env("CARD_BANK_NAME", "بانک ملی ایران"))

    @CARD_BANK_NAME.setter
    def CARD_BANK_NAME(self, val: str):
        update_env_variable("CARD_BANK_NAME", val)

    @property
    def CARD_IBAN(self) -> str:
        return str(get_live_env("CARD_IBAN", "IR000000000000000000000000"))

    @CARD_IBAN.setter
    def CARD_IBAN(self, val: str):
        update_env_variable("CARD_IBAN", val)

    @property
    def CARD_TO_CARD_ENABLED(self) -> bool:
        v = get_live_env("CARD_TO_CARD_ENABLED", "false")
        return str(v).lower() in ("true", "1", "yes", "on")

    @CARD_TO_CARD_ENABLED.setter
    def CARD_TO_CARD_ENABLED(self, val: bool):
        update_env_variable("CARD_TO_CARD_ENABLED", str(val).lower())

    @property
    def TRIAL_ENABLED(self) -> bool:
        v = get_live_env("TRIAL_ENABLED", "true")
        return str(v).lower() in ("true", "1", "yes", "on")

    @TRIAL_ENABLED.setter
    def TRIAL_ENABLED(self, val: bool):
        update_env_variable("TRIAL_ENABLED", str(val).lower())

    @property
    def TRIAL_TRAFFIC_MB(self) -> int:
        v = get_live_env("TRIAL_TRAFFIC_MB", 1024)
        try:
            return int(v)
        except Exception:
            return 1024

    @TRIAL_TRAFFIC_MB.setter
    def TRIAL_TRAFFIC_MB(self, val: int):
        update_env_variable("TRIAL_TRAFFIC_MB", str(val))

    @property
    def TRIAL_DURATION_DAYS(self) -> int:
        v = get_live_env("TRIAL_DURATION_DAYS", 1)
        try:
            return int(v)
        except Exception:
            return 1

    @TRIAL_DURATION_DAYS.setter
    def TRIAL_DURATION_DAYS(self, val: int):
        update_env_variable("TRIAL_DURATION_DAYS", str(val))

    @property
    def TRIAL_DURATION_HOURS(self) -> int:
        v = get_live_env("TRIAL_DURATION_HOURS", self.TRIAL_DURATION_DAYS * 24)
        try:
            return int(v)
        except Exception:
            return self.TRIAL_DURATION_DAYS * 24

    @TRIAL_DURATION_HOURS.setter
    def TRIAL_DURATION_HOURS(self, val: int):
        update_env_variable("TRIAL_DURATION_HOURS", str(val))

    @property
    def TRIAL_MAX_COUNT(self) -> int:
        v = get_live_env("TRIAL_MAX_COUNT", 1)
        try:
            return int(v)
        except Exception:
            return 1

    @TRIAL_MAX_COUNT.setter
    def TRIAL_MAX_COUNT(self, val: int):
        update_env_variable("TRIAL_MAX_COUNT", str(val))

    @property
    def TRIAL_INBOUND_ID(self) -> str | None:
        v = get_live_env("TRIAL_INBOUND_ID", None)
        return str(v) if v and str(v).strip() and str(v).lower() != "none" else None

    @TRIAL_INBOUND_ID.setter
    def TRIAL_INBOUND_ID(self, val: str | None):
        update_env_variable("TRIAL_INBOUND_ID", val or "")

    @property
    def MANDATORY_CHANNEL_ENABLED(self) -> bool:
        v = get_live_env("MANDATORY_CHANNEL_ENABLED", "true")
        return str(v).lower() in ("true", "1", "yes", "on")

    @MANDATORY_CHANNEL_ENABLED.setter
    def MANDATORY_CHANNEL_ENABLED(self, val: bool):
        update_env_variable("MANDATORY_CHANNEL_ENABLED", str(val).lower())

    @property
    def CHANNEL_USERNAME(self) -> str:
        return str(get_live_env("CHANNEL_USERNAME", "ADVPN_Private")).strip().lstrip("@")

    @CHANNEL_USERNAME.setter
    def CHANNEL_USERNAME(self, val: str):
        update_env_variable("CHANNEL_USERNAME", str(val).strip().lstrip("@"))

    @property
    def SUPPORT_USERNAME(self) -> str:
        return str(get_live_env("SUPPORT_USERNAME", "VPNPrivateSupport")).strip().lstrip("@")

    @SUPPORT_USERNAME.setter
    def SUPPORT_USERNAME(self, val: str):
        update_env_variable("SUPPORT_USERNAME", str(val).strip().lstrip("@"))

    @property
    def CRYPTO_WALLET_TRC20(self) -> str:
        v = get_live_env("CRYPTO_WALLET_TRC20", get_live_env("CRYPTO_WALLET_USDT", "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"))
        return str(v)

    @CRYPTO_WALLET_TRC20.setter
    def CRYPTO_WALLET_TRC20(self, val: str):
        val_str = str(val).strip()
        update_env_variable("CRYPTO_WALLET_TRC20", val_str)
        update_env_variable("CRYPTO_WALLET_USDT", val_str)

    @property
    def CRYPTO_WALLET_BEP20(self) -> str:
        v = get_live_env("CRYPTO_WALLET_BEP20", "0x587EF4ea392c6947688533fb0550Bae94B8f0E8a")
        return str(v)

    @CRYPTO_WALLET_BEP20.setter
    def CRYPTO_WALLET_BEP20(self, val: str):
        val_str = str(val).strip()
        update_env_variable("CRYPTO_WALLET_BEP20", val_str)

    @property
    def CRYPTO_TRC20_ENABLED(self) -> bool:
        v = get_live_env("CRYPTO_TRC20_ENABLED", "true")
        return str(v).lower() in ("true", "1", "yes", "on")

    @CRYPTO_TRC20_ENABLED.setter
    def CRYPTO_TRC20_ENABLED(self, val: bool):
        update_env_variable("CRYPTO_TRC20_ENABLED", str(val).lower())

    @property
    def CRYPTO_BEP20_ENABLED(self) -> bool:
        v = get_live_env("CRYPTO_BEP20_ENABLED", "false")
        return str(v).lower() in ("true", "1", "yes", "on")

    @CRYPTO_BEP20_ENABLED.setter
    def CRYPTO_BEP20_ENABLED(self, val: bool):
        update_env_variable("CRYPTO_BEP20_ENABLED", str(val).lower())

    @property
    def CRYPTO_WALLET_USDT(self) -> str:
        v = get_live_env("CRYPTO_WALLET_USDT", get_live_env("CRYPTO_WALLET_TRC20", "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"))
        return str(v)

    @CRYPTO_WALLET_USDT.setter
    def CRYPTO_WALLET_USDT(self, val: str):
        val_str = str(val).strip()
        update_env_variable("CRYPTO_WALLET_USDT", val_str)
        update_env_variable("CRYPTO_WALLET_TRC20", val_str)

    @property
    def CRYPTO_ENABLED(self) -> bool:
        v = get_live_env("CRYPTO_ENABLED", "false")
        return str(v).lower() in ("true", "1", "yes", "on")

    @CRYPTO_ENABLED.setter
    def CRYPTO_ENABLED(self, val: bool):
        update_env_variable("CRYPTO_ENABLED", str(val).lower())

    @property
    def USDT_TOMAN_RATE(self) -> int:
        v = get_live_env("USDT_TOMAN_RATE", 70000)
        try:
            return int(v)
        except Exception:
            return 70000

    @USDT_TOMAN_RATE.setter
    def USDT_TOMAN_RATE(self, val: int):
        update_env_variable("USDT_TOMAN_RATE", str(val))

    @property
    def USDT_TRC20_RATE(self) -> float:
        v = get_live_env("USDT_TRC20_RATE", self.USDT_TOMAN_RATE)
        try:
            return float(v)
        except Exception:
            return float(self.USDT_TOMAN_RATE)

    @USDT_TRC20_RATE.setter
    def USDT_TRC20_RATE(self, val: float | int | str):
        update_env_variable("USDT_TRC20_RATE", str(val))

    @property
    def ZARINPAL_ENABLED(self) -> bool:
        v = get_live_env("ZARINPAL_ENABLED", "false")
        return str(v).lower() in ("true", "1", "yes", "on")

    @ZARINPAL_ENABLED.setter
    def ZARINPAL_ENABLED(self, val: bool):
        update_env_variable("ZARINPAL_ENABLED", str(val).lower())

    @property
    def ZARINPAL_MERCHANT_ID(self) -> str:
        return str(get_live_env("ZARINPAL_MERCHANT_ID", "00000000-0000-0000-0000-000000000000"))

    @ZARINPAL_MERCHANT_ID.setter
    def ZARINPAL_MERCHANT_ID(self, val: str):
        update_env_variable("ZARINPAL_MERCHANT_ID", str(val).strip())

    @property
    def STRIPE_ENABLED(self) -> bool:
        v = get_live_env("STRIPE_ENABLED", "false")
        return str(v).lower() in ("true", "1", "yes", "on")

    @STRIPE_ENABLED.setter
    def STRIPE_ENABLED(self, val: bool):
        update_env_variable("STRIPE_ENABLED", str(val).lower())

    @property
    def STRIPE_PUBLISHABLE_KEY(self) -> str:
        return str(get_live_env("STRIPE_PUBLISHABLE_KEY", ""))

    @STRIPE_PUBLISHABLE_KEY.setter
    def STRIPE_PUBLISHABLE_KEY(self, val: str):
        update_env_variable("STRIPE_PUBLISHABLE_KEY", str(val).strip())

    @property
    def STRIPE_SECRET_KEY(self) -> str:
        return str(get_live_env("STRIPE_SECRET_KEY", ""))

    @STRIPE_SECRET_KEY.setter
    def STRIPE_SECRET_KEY(self, val: str):
        update_env_variable("STRIPE_SECRET_KEY", str(val).strip())

    @property
    def STRIPE_CURRENCY(self) -> str:
        return str(get_live_env("STRIPE_CURRENCY", "USD")).upper()

    @STRIPE_CURRENCY.setter
    def STRIPE_CURRENCY(self, val: str):
        update_env_variable("STRIPE_CURRENCY", str(val).strip().upper())

    @property
    def STRIPE_EXCHANGE_RATE(self) -> int:
        v = get_live_env("STRIPE_EXCHANGE_RATE", 70000)
        try:
            return int(v)
        except Exception:
            return 70000

    @STRIPE_EXCHANGE_RATE.setter
    def STRIPE_EXCHANGE_RATE(self, val: int):
        update_env_variable("STRIPE_EXCHANGE_RATE", str(val))

    @property
    def MIN_WALLET_TOPUP_TOMAN(self) -> int:
        v = get_live_env("MIN_WALLET_TOPUP_TOMAN", 10000)
        try:
            return int(v)
        except Exception:
            return 10000

    @MIN_WALLET_TOPUP_TOMAN.setter
    def MIN_WALLET_TOPUP_TOMAN(self, val: int):
        update_env_variable("MIN_WALLET_TOPUP_TOMAN", str(val))

    @property
    def WALLET_ENABLED(self) -> bool:
        v = get_live_env("WALLET_ENABLED", "true")
        return str(v).lower() in ("true", "1", "yes", "on")

    @WALLET_ENABLED.setter
    def WALLET_ENABLED(self, val: bool):
        update_env_variable("WALLET_ENABLED", str(val).lower() if isinstance(val, bool) else str(val))

    @property
    def REFERRAL_COMMISSION_PERCENT(self) -> int:
        v = get_live_env("REFERRAL_COMMISSION_PERCENT", 10)
        try:
            return int(v)
        except Exception:
            return 10

    @REFERRAL_COMMISSION_PERCENT.setter
    def REFERRAL_COMMISSION_PERCENT(self, val: int):
        update_env_variable("REFERRAL_COMMISSION_PERCENT", str(val))

    # =========================================================
    # WEB ADMIN CREDENTIALS & 2FA SECURITY (SYNCED WITH .ENV)
    # =========================================================
    @property
    def ADMIN_WEB_USERNAME(self) -> str:
        return str(get_live_env("ADMIN_WEB_USERNAME", "admin"))

    @ADMIN_WEB_USERNAME.setter
    def ADMIN_WEB_USERNAME(self, val: str):
        update_env_variable("ADMIN_WEB_USERNAME", str(val).strip())

    @property
    def ADMIN_WEB_PASSWORD(self) -> str:
        return str(get_live_env("ADMIN_WEB_PASSWORD", "admin"))

    @ADMIN_WEB_PASSWORD.setter
    def ADMIN_WEB_PASSWORD(self, val: str):
        update_env_variable("ADMIN_WEB_PASSWORD", str(val))

    @property
    def ADMIN_WEB_PASSWORD_HASH(self) -> str:
        val = str(get_live_env("ADMIN_WEB_PASSWORD_HASH", ""))
        if "$$" in val:
            val = val.replace("$$", "$")
        return val

    @ADMIN_WEB_PASSWORD_HASH.setter
    def ADMIN_WEB_PASSWORD_HASH(self, val: str):
        update_env_variable("ADMIN_WEB_PASSWORD_HASH", str(val))

    @property
    def ADMIN_2FA_ENABLED(self) -> bool:
        v = get_live_env("ADMIN_2FA_ENABLED", "false")
        return str(v).lower() in ("true", "1", "yes", "on")

    @ADMIN_2FA_ENABLED.setter
    def ADMIN_2FA_ENABLED(self, val: bool):
        update_env_variable("ADMIN_2FA_ENABLED", str(val).lower())

    @property
    def ADMIN_2FA_TELEGRAM_ID(self) -> int:
        v = get_live_env("ADMIN_2FA_TELEGRAM_ID", 0)
        try:
            return int(v)
        except Exception:
            return 0

    @ADMIN_2FA_TELEGRAM_ID.setter
    def ADMIN_2FA_TELEGRAM_ID(self, val: int):
        update_env_variable("ADMIN_2FA_TELEGRAM_ID", str(val))

    @property
    def ADMIN_TOTP_SECRET(self) -> str:
        return str(get_live_env("ADMIN_TOTP_SECRET", ""))

    @ADMIN_TOTP_SECRET.setter
    def ADMIN_TOTP_SECRET(self, val: str):
        update_env_variable("ADMIN_TOTP_SECRET", str(val).strip())

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in self.__dict__:
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        val = get_live_env(name, None)
        if val is not None:
            return val
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")



settings = Settings()
