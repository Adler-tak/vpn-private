"""Authentication and User Context Middleware for Aiogram 3."""
import logging
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser, Message
from app.database.session import AsyncSessionLocal
from app.modules.users.service import UserService

logger = logging.getLogger(__name__)


class UserAuthMiddleware(BaseMiddleware):
    """Ensures every interacting Telegram user is saved in DB and provides DB session & user context."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user: TgUser = data.get("event_from_user")
        if not tg_user:
            return await handler(event, data)

        # Extract referral argument if message is /start <ref_code>
        referrer_code = None
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            parts = event.text.split(maxsplit=1)
            if len(parts) > 1:
                arg = parts[1].strip()
                if not (arg.startswith("v_") or arg.startswith("v-")):
                    referrer_code = arg

        async with AsyncSessionLocal() as session:
            try:
                user_service = UserService(session)
                user, created = await user_service.register_or_get_user(
                    telegram_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                    referrer_code=referrer_code,
                )
                if user.is_banned:
                    logger.warning(f"Banned user {tg_user.id} attempted interaction.")
                    return None

                data["db_user"] = user
                data["db_session"] = session
                data["is_new_user"] = created

                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
