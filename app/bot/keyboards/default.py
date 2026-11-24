"""Default Persian Reply Keyboards."""
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from app.bot.locales.fa import MAIN_MENU_BTNS
from app.core.config import settings


def get_effective_webapp_url() -> str:
    """Retrieve effective Mini App HTTPS URL from dynamic settings, env, or fallback."""
    try:
        from app.services.settings_service import SettingsService
        url = SettingsService.get_value("WEBAPP_URL", "") or getattr(settings, "WEBAPP_URL", "") or ""
        if not url:
            host = SettingsService.get_value("SERVER_HOST", "") or getattr(settings, "SERVER_HOST", "") or ""
            if host:
                url = f"{host.rstrip('/')}/webapp"
        return url
    except Exception:
        return getattr(settings, "WEBAPP_URL", "") or f"{settings.SERVER_HOST}/webapp"


def get_main_reply_keyboard(is_admin: bool = False, is_reseller: bool = False) -> ReplyKeyboardMarkup:
    """Build the main customer menu keyboard."""
    webapp_url = get_effective_webapp_url()
    is_valid_webapp = (
        webapp_url.startswith("https://")
        or "localhost" in webapp_url
        or "127.0.0.1" in webapp_url
    )

    keyboard = []
    if is_valid_webapp:
        keyboard.append([
            KeyboardButton(text="App", web_app=WebAppInfo(url=webapp_url)),
        ])

    keyboard.extend([
        [
            KeyboardButton(text=MAIN_MENU_BTNS["buy"]),
            KeyboardButton(text=MAIN_MENU_BTNS["my_services"]),
        ],
        [
            KeyboardButton(text=MAIN_MENU_BTNS["renew"]),
            KeyboardButton(text=MAIN_MENU_BTNS["usage"]),
        ],
        [
            KeyboardButton(text=MAIN_MENU_BTNS["wallet"]),
            KeyboardButton(text=MAIN_MENU_BTNS["voucher"]),
        ],
        [
            KeyboardButton(text=MAIN_MENU_BTNS["lucky_wheel"]),
            KeyboardButton(text=MAIN_MENU_BTNS["gift_card"]),
        ],
        [
            KeyboardButton(text=MAIN_MENU_BTNS["trial"]),
            KeyboardButton(text=MAIN_MENU_BTNS["referral"]),
        ],
        [
            KeyboardButton(text=MAIN_MENU_BTNS["support"]),
            KeyboardButton(text=MAIN_MENU_BTNS["tutorial"]),
        ],
        [
            KeyboardButton(text=MAIN_MENU_BTNS["rules"]),
        ],
    ])
    admin_row = []
    if is_reseller:
        admin_row.append(KeyboardButton(text=MAIN_MENU_BTNS["reseller"]))
    if is_admin:
        admin_row.append(KeyboardButton(text=MAIN_MENU_BTNS["admin"]))
    if admin_row:
        keyboard.append(admin_row)

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, is_persistent=True)


def get_user_reply_keyboard(user: object) -> ReplyKeyboardMarkup:
    """Build main reply keyboard for user, respecting admin/reseller privileges."""
    from app.core.config import settings
    from app.core.constants import UserRole

    tg_id = getattr(user, "telegram_id", None)
    role = getattr(user, "role", None)

    is_admin = False
    if tg_id is not None and tg_id in settings.ADMIN_TELEGRAM_IDS:
        is_admin = True
    elif role in (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.FINANCE, UserRole.SUPPORT):
        is_admin = True

    is_reseller = (role == UserRole.RESELLER)
    return get_main_reply_keyboard(is_admin=is_admin, is_reseller=is_reseller)
