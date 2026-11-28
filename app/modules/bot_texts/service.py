"""Dynamic Bot Texts Service with zero-overhead mtime file sync."""
import json
import logging
import os
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
PRIMARY_BOT_TEXTS_FILE = APP_DIR / "bot_texts.json"
DATA_DIR = APP_DIR.parent / "data"
DATA_BOT_TEXTS_FILE = DATA_DIR / "bot_texts.json"

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


def _load_texts() -> Dict[str, str]:
    """Read texts from bot_texts.json checking both primary (mounted /app/app) and data directories."""
    global _cache, _cache_mtime
    
    check_files = [BOT_TEXTS_FILE] if BOT_TEXTS_FILE not in (PRIMARY_BOT_TEXTS_FILE, DATA_BOT_TEXTS_FILE) else [PRIMARY_BOT_TEXTS_FILE, DATA_BOT_TEXTS_FILE]

    # Check which file is newer or exists
    target_file = None
    max_mtime = 0.0
    for f in check_files:
        if f.exists():
            try:
                mt = os.path.getmtime(f)
                if mt >= max_mtime:
                    max_mtime = mt
                    target_file = f
            except Exception:
                pass

    if target_file is None:
        try:
            out_file = check_files[0]
            out_file.parent.mkdir(parents=True, exist_ok=True)
            initial = {k: v["default"] for k, v in DEFAULT_BOT_TEXTS.items()}
            initial_json = json.dumps(initial, ensure_ascii=False, indent=2)
            out_file.write_text(initial_json, encoding="utf-8")
            if len(check_files) > 1:
                try:
                    check_files[1].parent.mkdir(parents=True, exist_ok=True)
                    check_files[1].write_text(initial_json, encoding="utf-8")
                except Exception:
                    pass
            _cache = initial
            _cache_mtime = os.path.getmtime(out_file)
            return _cache
        except Exception as e:
            logger.warning(f"Could not initialize bot_texts.json: {e}")
            return {k: v["default"] for k, v in DEFAULT_BOT_TEXTS.items()}

    try:
        curr_mtime = max_mtime
        if curr_mtime != _cache_mtime:
            data = json.loads(target_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _cache = data
                _cache_mtime = curr_mtime
    except Exception as e:
        logger.warning(f"Error reading bot_texts.json from {target_file}: {e}")

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
    """Atomically update bot texts in bot_texts.json in both mounted and data paths."""
    global _cache, _cache_mtime
    check_files = [BOT_TEXTS_FILE] if BOT_TEXTS_FILE not in (PRIMARY_BOT_TEXTS_FILE, DATA_BOT_TEXTS_FILE) else [PRIMARY_BOT_TEXTS_FILE, DATA_BOT_TEXTS_FILE]

    current = {}
    for f in check_files:
        if f.exists():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    current.update(raw)
                    break
            except Exception:
                pass

    current.update({str(k): str(v) for k, v in texts.items() if v is not None})
    json_str = json.dumps(current, ensure_ascii=False, indent=2)

    for f in check_files:
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json_str, encoding="utf-8")
            _cache_mtime = os.path.getmtime(f)
        except Exception as e:
            logger.debug(f"Could not write {f}: {e}")

    _cache = current
    logger.info(f"Updated {len(texts)} bot texts successfully.")
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
