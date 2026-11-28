"""Dynamic Bot Texts Service with database (system_settings) persistence and cross-container sync."""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Canonical default texts from fa locale
try:
    from app.bot.locales.fa import WELCOME_MSG, RULES_TEXT
except ImportError:
    WELCOME_MSG = "به ربات VPN Private خوش آمدید!"
    RULES_TEXT = "قوانین و شرایط استفاده از سرویس."

APP_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = APP_DIR.parent
PRIMARY_BOT_TEXTS_FILE = APP_DIR / "bot_texts.json"
DATA_DIR = PROJECT_ROOT / "data"
DATA_BOT_TEXTS_FILE = DATA_DIR / "bot_texts.json"
DB_FILE = PROJECT_ROOT / "vpn_private.db"

BOT_TEXTS_FILE = PRIMARY_BOT_TEXTS_FILE

DEFAULT_BOT_TEXTS: Dict[str, Dict[str, str]] = {
    "start_welcome": {
        "title": "پیام شروع و خوش‌آمدگویی",
        "description": "پیام خوش‌آمدگویی اصلی ربات تلگرام هنگام زدن /start و بازگشت به منو",
        "default": WELCOME_MSG.strip(),
    },
    "purchase_guide": {
        "title": "راهنمای خرید اشتراک",
        "description": "پیام راهنما و عنوان منوی انتخاب پلن‌های خرید اشتراک اینترنت آزاد",
        "default": "🛒 <b>لطفاً یکی از بسته‌های اینترنت آزاد زیر را انتخاب فرمایید:</b>",
    },
    "services_guide": {
        "title": "راهنمای سرویس‌های من",
        "description": "متن راهنما و وضعیت سرویس‌های فعال در منوی سرویس‌های من",
        "default": "📦 <b>سرویس‌های فعال شما:</b>\n\nدر این بخش می‌توانید مشخصات، حجم مصرفی و وضعیت اتصال اشتراک‌های خود را مشاهده و مدیریت فرمایید.",
    },
    "wallet_guide": {
        "title": "راهنمای کیف پول",
        "description": "متن توضیحات و راهنمای افزایش اعتبار و موجودی کیف پول کاربری",
        "default": "با شارژ کیف پول یا ثبت کارت‌های ووچر هدیه، می‌توانید بدون نیاز به کارت به کارت در هر زمان سرویس‌های خود را آنی خریداری و تمدید نمایید.",
    },
    "card_payment_guide": {
        "title": "راهنمای کارت به کارت و ارسال رسید",
        "description": "دستورالعمل واریز کارت به کارت و ارسال تصویر فیش بانکی",
        "default": "📸 <b>مرحله نهایی: لطفاً تصویر فیش یا رسید واریزی خود را در همین بخش ارسال نمایید:</b>\n<i>(کافیست عکس رسید را مستقیماً مانند یک عکس یا پیام عادی به همین چت بفرستید)</i>",
    },
    "referral_guide": {
        "title": "متن پیام دعوت از دوستان و رفرال",
        "description": "قالب پیام ارسالی به کاربر هنگام کلیک روی دکمه «دعوت از دوستان». متغیرها: {referral_link}، {commission_percent}، {invited_count}، {commission_toman}، {bot_username}",
        "default": (
            "👥 <b>سیستم همکاری در فروش و دعوت از دوستان:</b>\n\n"
            "با دعوت از دوستان خود به ربات، <b>{commission_percent}٪</b> از هر خرید آن‌ها به صورت خودکار به کیف پول شما واریز می‌شود!\n\n"
            "🔗 <b>لینک دعوت اختصاصی شما:</b>\n"
            "<code>{referral_link}</code>\n\n"
            "📊 <b>آمار فعالیت شما:</b>\n"
            "• تعداد دوستان دعوت‌شده: <b>{invited_count} نفر</b>\n"
            "• پاداش کسب‌شده: <b>{commission_toman} تومان</b>\n\n"
            "💡 <i>پاداش‌های شما در کیف پول ذخیره شده و می‌توانید با آن سرویس رایگان تهیه فرمایید یا موجودی خود را افزایش دهید.</i>"
        ),
    },
    "support_guide": {
        "title": "راهنمای پشتیبانی و تیکتینگ",
        "description": "متن راهنما و خوش‌آمدگویی مرکز تیکتینگ و پشتیبانی کاربران",
        "default": "🎧 <b>مرکز پشتیبانی VPN Private:</b>\n\nتیم پشتیبانی ما به صورت ۲۴ ساعته آماده پاسخگویی به سوالات و رفع مشکلات شما می‌باشد.",
    },
    "rules_terms": {
        "title": "قوانین و شرایط استفاده",
        "description": "متن قوانین سرویس، حریم خصوصی و شرایط استفاده کاربران",
        "default": RULES_TEXT.strip(),
    },
    "free_trial_guide": {
        "title": "راهنمای اکانت تست رایگان",
        "description": "راهنمای اتصال و استفاده از اشتراک تست رایگان",
        "default": "💡 <i>لینک فوق را کپی کرده یا بارکد QR را درون اپلیکیشن Happ، Streisand یا v2rayNG اسکن فرمایید.</i>",
    },
    "order_approved_delivery": {
        "title": "متن تایید پرداخت و تحویل کانفیگ به کاربر",
        "description": "متنی که پس از تایید فیش توسط ادمین به همراه بارکد QR برای کاربر ارسال می‌شود. متغیرها: {client_email}, {location}, {flag}, {protocol}, {subscription_url}, {support_username}, {remark}",
        "default": (
            "🎉 <b>پرداخت شما تایید شد و سرویس فعال گردید!</b>\n\n"
            "🏷 <b>شناسه:</b> <code>{client_email}</code>\n"
            "🌍 <b>لوکیشن:</b> {flag} <b>{location}</b>\n"
            "📡 <b>پروتکل:</b> <b>{protocol}</b>\n\n"
            "🔗 <b>لینک اشتراک هوشمند (Subscription URL):</b>\n"
            "<code>{subscription_url}</code>\n\n"
            "📱 <b>نرم‌افزار پیشنهادی جهت اتصال (Happ Proxy):</b>\n"
            "🍏 <b>دانلود نسخه iOS (آیفون):</b> <a href=\"https://apps.apple.com/app/happ-proxy-utility/id6504287215\">دریافت از App Store</a>\n"
            "🤖 <b>دانلود نسخه اندروید:</b> <a href=\"https://play.google.com/store/apps/details?id=com.happproxy\">دریافت از Google Play</a>\n\n"
            "💡 <i>کافیست پس از نصب برنامه، لینک اشتراک هوشمند بالا را کپی کرده یا بارکد QR بالا را درون نرم‌افزار اسکن نمایید.</i>\n\n"
            "🙏 از اعتماد شما به VPN Private سپاسگزاریم."
        ),
    },
}

_cache: Dict[str, str] = {}
_cache_mtime: float = 0.0
_last_db_mtime: float = 0.0
_last_sync_check: float = 0.0


def _read_json_files() -> Dict[str, str]:
    """Read newest existing JSON file backup."""
    check_files = [BOT_TEXTS_FILE] if BOT_TEXTS_FILE not in (PRIMARY_BOT_TEXTS_FILE, DATA_BOT_TEXTS_FILE) else [PRIMARY_BOT_TEXTS_FILE, DATA_BOT_TEXTS_FILE]
    best_file = None
    max_mtime = 0.0
    for f in check_files:
        if f.exists():
            try:
                mt = os.path.getmtime(f)
                if mt >= max_mtime:
                    max_mtime = mt
                    best_file = f
            except Exception:
                pass
    if best_file:
        try:
            data = json.loads(best_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.debug(f"Error reading JSON from {best_file}: {e}")
    return {}


def _write_json_files(texts: Dict[str, str]) -> None:
    """Write texts to JSON files as local and volume backups."""
    json_str = json.dumps(texts, ensure_ascii=False, indent=2)
    check_files = [BOT_TEXTS_FILE] if BOT_TEXTS_FILE not in (PRIMARY_BOT_TEXTS_FILE, DATA_BOT_TEXTS_FILE) else [PRIMARY_BOT_TEXTS_FILE, DATA_BOT_TEXTS_FILE]
    for f in check_files:
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json_str, encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not write {f}: {e}")


def _load_from_db() -> Dict[str, str]:
    """Load bot texts directly from the database (system_settings table)."""
    db_data: Dict[str, str] = {}
    try:
        from app.database.session import sync_session_factory
        from app.database.models.setting import SystemSetting
        from sqlalchemy import select

        with sync_session_factory() as session:
            res = session.execute(
                select(SystemSetting).where(SystemSetting.category == "bot_texts")
            )
            rows = res.scalars().all()
            for row in rows:
                if row.key and row.value is not None:
                    db_data[row.key] = row.value

            # If DB is empty, migrate from JSON backup or defaults
            if not db_data:
                json_data = _read_json_files()
                to_add = []
                for k, meta in DEFAULT_BOT_TEXTS.items():
                    val = json_data.get(k, meta["default"])
                    db_data[k] = val
                    to_add.append(
                        SystemSetting(
                            key=k,
                            value=val,
                            category="bot_texts",
                            description=meta["description"],
                        )
                    )
                if to_add:
                    session.add_all(to_add)
                    session.commit()
            else:
                # Seed any missing default keys into DB
                missing = [k for k in DEFAULT_BOT_TEXTS if k not in db_data]
                if missing:
                    to_add = []
                    for k in missing:
                        meta = DEFAULT_BOT_TEXTS[k]
                        val = meta["default"]
                        db_data[k] = val
                        to_add.append(
                            SystemSetting(
                                key=k,
                                value=val,
                                category="bot_texts",
                                description=meta["description"],
                            )
                        )
                    session.add_all(to_add)
                    session.commit()
    except Exception as e:
        logger.debug(f"Could not load bot texts from DB: {e}")
    return db_data


def _load_texts() -> Dict[str, str]:
    """Load texts from database with in-memory caching and cross-container mtime sync."""
    global _cache, _cache_mtime, _last_db_mtime, _last_sync_check
    now = time.monotonic()

    # Rate-limit disk/DB checks to once per second if cache is warm
    if _cache and (now - _last_sync_check) < 1.0:
        return _cache
    _last_sync_check = now

    # Determine DB mtime
    db_mtime = 0.0
    for p in (DB_FILE, APP_DIR / "vpn_private.db", Path("vpn_private.db")):
        if p.exists():
            try:
                db_mtime = max(db_mtime, os.path.getmtime(p))
            except Exception:
                pass

    if _cache and db_mtime > 0 and db_mtime == _last_db_mtime:
        return _cache

    # 1. Primary: Load from DB
    db_texts = _load_from_db()
    if db_texts:
        _cache = db_texts
        _last_db_mtime = db_mtime
        _write_json_files(_cache)
        return _cache

    # 2. Secondary: Fallback to JSON files
    json_texts = _read_json_files()
    if json_texts:
        _cache = {**{k: v["default"] for k, v in DEFAULT_BOT_TEXTS.items()}, **json_texts}
        return _cache

    # 3. Default fallback
    _cache = {k: v["default"] for k, v in DEFAULT_BOT_TEXTS.items()}
    return _cache


def get_bot_text(key: str, default: Optional[str] = None) -> str:
    """Retrieve dynamic bot text by canonical key with dynamic placeholder interpolation."""
    texts = _load_texts()
    val = ""
    if key in texts and texts[key] is not None and str(texts[key]).strip():
        val = str(texts[key])
    elif key in DEFAULT_BOT_TEXTS:
        val = DEFAULT_BOT_TEXTS[key]["default"]
    else:
        val = default or ""

    try:
        from app.core.config import settings
        from app.services.settings_service import get_effective_bot_username_sync
        ch = getattr(settings, "CHANNEL_USERNAME", "ADVPN_Private").lstrip("@")
        sup = getattr(settings, "SUPPORT_USERNAME", "VPNPrivateSupport").lstrip("@")
        bot = get_effective_bot_username_sync().lstrip("@")
        if "@{channel_username}" in val:
            val = val.replace("@{channel_username}", f"@{ch}")
        if "{channel_username}" in val:
            val = val.replace("{channel_username}", f"@{ch}")

        if "@{support_username}" in val:
            val = val.replace("@{support_username}", f"@{sup}")
        if "{support_username}" in val:
            val = val.replace("{support_username}", f"@{sup}")

        if "@{bot_username}" in val:
            val = val.replace("@{bot_username}", f"@{bot}")
        if "{bot_username}" in val:
            val = val.replace("{bot_username}", f"@{bot}")

        # Clean any accidental double @@
        val = val.replace("@@", "@")
    except Exception:
        pass

    return val


def update_bot_texts(texts: Dict[str, str]) -> Dict[str, str]:
    """Persist bot texts into database (system_settings) and file backups."""
    global _cache, _last_db_mtime, _last_sync_check
    clean = {str(k): str(v) for k, v in texts.items() if v is not None}

    # 1. Update in-memory cache
    if not _cache:
        _load_texts()
    _cache.update(clean)

    # 2. Persist to database (system_settings table)
    try:
        from app.database.session import sync_session_factory
        from app.database.models.setting import SystemSetting

        with sync_session_factory() as session:
            for k, v in clean.items():
                setting = session.get(SystemSetting, k)
                if setting:
                    setting.value = v
                    setting.category = "bot_texts"
                else:
                    desc = DEFAULT_BOT_TEXTS.get(k, {}).get("description", "متن سفارشی ربات")
                    session.add(
                        SystemSetting(
                            key=k,
                            value=v,
                            category="bot_texts",
                            description=desc,
                        )
                    )
            session.commit()
    except Exception as e:
        logger.warning(f"Could not persist bot texts to database: {e}")

    # 3. Write to JSON files for backup
    _write_json_files(_cache)

    # Refresh DB mtime tracking
    for p in (DB_FILE, APP_DIR / "vpn_private.db", Path("vpn_private.db")):
        if p.exists():
            try:
                _last_db_mtime = os.path.getmtime(p)
                break
            except Exception:
                pass

    logger.info(f"Updated {len(clean)} bot texts in database and files.")
    return _cache


def get_all_bot_texts() -> List[Dict[str, Any]]:
    """Return all defined bot texts with metadata (key, title, description, current_value)."""
    texts = _load_texts()
    result = []
    seen = set()

    for key, meta in DEFAULT_BOT_TEXTS.items():
        seen.add(key)
        result.append({
            "key": key,
            "title": meta["title"],
            "description": meta["description"],
            "current_value": texts.get(key, meta["default"]),
            "default_value": meta["default"],
        })

    for key, val in texts.items():
        if key not in seen:
            result.append({
                "key": key,
                "title": key,
                "description": "متن سفارشی ربات",
                "current_value": str(val),
                "default_value": str(val),
            })

    return result
