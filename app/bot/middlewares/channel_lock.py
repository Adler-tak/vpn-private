"""Mandatory Telegram Channel Join Middleware."""
import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from app.core.config import settings

logger = logging.getLogger(__name__)


class MandatoryChannelJoinMiddleware(BaseMiddleware):
    """
    Blocks non-admin users from interacting with the bot if MANDATORY_CHANNEL_ENABLED is True
    and the user has not joined settings.CHANNEL_USERNAME.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # If mandatory channel join is disabled, bypass check
        if not getattr(settings, "MANDATORY_CHANNEL_ENABLED", False):
            return await handler(event, data)

        channel = getattr(settings, "CHANNEL_USERNAME", "").strip().lstrip("@")
        if not channel:
            return await handler(event, data)

        event_user = data.get("event_from_user")
        if not event_user:
            return await handler(event, data)

        # Allow /admin command and admin callbacks even if not in channel so admin is never locked out of panel
        is_admin_action = False
        if isinstance(event, Message) and event.text and event.text.startswith("/admin"):
            is_admin_action = True
        elif isinstance(event, CallbackQuery) and event.data and event.data.startswith("admin_"):
            is_admin_action = True

        admin_ids = [int(a) for a in getattr(settings, "ADMIN_TELEGRAM_IDS", []) if str(a).isdigit()]
        if is_admin_action and event_user.id in admin_ids:
            return await handler(event, data)

        # Allow check_channel_membership callback query to reach its dedicated handler
        if isinstance(event, CallbackQuery) and event.data == "check_channel_membership":
            return await handler(event, data)

        bot: Bot = data.get("bot")
        if not bot:
            return await handler(event, data)

        # Check membership
        is_member = await self._is_channel_member(bot, f"@{channel}", event_user.id)
        if is_member:
            return await handler(event, data)

        # Not a member -> show join channel prompt
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 عضویت در کانال رسمی", url=f"https://t.me/{channel}")],
            [InlineKeyboardButton(text="🔄 بررسی و تایید عضویت", callback_data="check_channel_membership")],
        ])
        msg_text = (
            f"🔒 <b>عضویت در کانال الزامی است</b>\n\n"
            f"کاربر گرامی، جهت استفاده از امکانات ربات و دریافت آخرین اخبار و اطلاعیه‌های سرورها، "
            f"لطفاً ابتدا در کانال رسمی ما عضو شوید:\n\n"
            f"📢 <b>کانال:</b> <code>@{channel}</code>\n\n"
            f"<i>پس از عضویت، دکمه «بررسی و تایید عضویت» را لمس نمایید.</i>"
        )

        if isinstance(event, CallbackQuery):
            await event.answer("⚠️ لطفاً ابتدا در کانال عضو شوید.", show_alert=True)
            try:
                await event.message.edit_text(msg_text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await event.message.answer(msg_text, reply_markup=kb, parse_mode="HTML")
            return None
        elif isinstance(event, Message):
            await event.answer(msg_text, reply_markup=kb, parse_mode="HTML")
            return None

        return None

    async def _is_channel_member(self, bot: Bot, channel_chat_id: str, user_id: int) -> bool:
        """Query Telegram API to check if user is in channel."""
        try:
            member = await bot.get_chat_member(chat_id=channel_chat_id, user_id=user_id)
            status = getattr(member, "status", "")
            return status in ("creator", "administrator", "member", "restricted")
        except Exception as e:
            logger.warning(
                f"Could not verify membership in {channel_chat_id} for user {user_id}: {e}. (Ensure bot is admin in channel)"
            )
            return False

