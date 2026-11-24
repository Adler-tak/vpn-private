"""Notification helpers for broadcasting administrative alerts."""
import logging
from typing import List, Optional, Set
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User
from app.core.constants import UserRole
from app.core.config import settings

logger = logging.getLogger(__name__)


async def get_all_admin_telegram_ids(db_session: AsyncSession) -> Set[int]:
    """Retrieve all admin telegram IDs from both static settings and DB roles."""
    admin_ids = set(settings.ADMIN_TELEGRAM_IDS)
    try:
        db_admins = (await db_session.execute(
            select(User).where(User.role.in_([
                UserRole.SUPER_ADMIN,
                UserRole.ADMIN,
                UserRole.FINANCE,
                UserRole.SUPPORT,
            ]))
        )).scalars().all()
        for a in db_admins:
            if a.telegram_id:
                admin_ids.add(a.telegram_id)
    except Exception as e_admins:
        logger.warning(f"Error querying DB admins: {e_admins}")
    return admin_ids


async def notify_all_admins(
    bot: Bot,
    db_session: AsyncSession,
    text: str,
    photo_file_id: Optional[str] = None,
    document_file_id: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    exclude_telegram_id: Optional[int] = None,
):
    """
    Broadcast an administrative alert to all authorized administrators.
    Falls back to text message if photo/doc send fails.
    """
    admin_ids = await get_all_admin_telegram_ids(db_session)
    for admin_tg_id in admin_ids:
        if exclude_telegram_id and admin_tg_id == exclude_telegram_id:
            continue
        try:
            if photo_file_id:
                await bot.send_photo(
                    chat_id=admin_tg_id,
                    photo=photo_file_id,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            elif document_file_id:
                await bot.send_document(
                    chat_id=admin_tg_id,
                    document=document_file_id,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=admin_tg_id,
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
        except Exception as exc:
            logger.error(f"Failed to deliver admin notification to {admin_tg_id}: {exc}")
            # Text fallback if media failed
            if photo_file_id or document_file_id:
                try:
                    await bot.send_message(
                        chat_id=admin_tg_id,
                        text=f"⚠️ {text}\n(تصویر یا فایل در دیتابیس ثبت شده است)",
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                except Exception as fb_exc:
                    logger.error(f"Fallback text notification failed for {admin_tg_id}: {fb_exc}")

