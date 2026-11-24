"""Referral and affiliate program handlers."""
from aiogram import Router, F
from aiogram.types import Message
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User
from app.database.models.reseller import Referral
from app.bot.locales.fa import MAIN_MENU_BTNS
from app.modules.bot_texts.service import get_bot_text
from app.core.config import settings

from app.services.settings_service import get_effective_bot_username_sync

router = Router()


@router.message(F.text == MAIN_MENU_BTNS["referral"])
async def show_referral_info(message: Message, db_user: User, db_session: AsyncSession):
    bot_username = get_effective_bot_username_sync()
    referral_link = f"https://t.me/{bot_username}?start={db_user.referral_code}"

    # Calculate statistics
    count_res = await db_session.execute(
        select(func.count(User.id)).where(User.referred_by_id == db_user.id)
    )
    invited_count = count_res.scalar() or 0

    comm_res = await db_session.execute(
        select(func.coalesce(func.sum(Referral.total_commission_irr), 0)).where(
            Referral.referrer_user_id == db_user.id
        )
    )
    commission_irr = comm_res.scalar() or 0

    referral_note = get_bot_text(
        "referral_guide",
        "💡 پاداش‌های شما در کیف پول ذخیره شده و می‌توانید با آن سرویس رایگان تهیه فرمایید یا موجودی خود را افزایش دهید.",
    )
    text = (
        f"👥 <b>سیستم همکاری در فروش و دعوت از دوستان:</b>\n\n"
        f"با دعوت از دوستان خود به VPN Private، <b>{settings.REFERRAL_COMMISSION_PERCENT}٪</b> از هر خرید آن‌ها به صورت خودکار به کیف پول شما واریز می‌شود!\n\n"
        f"🔗 <b>لینک دعوت اختصاصی شما:</b>\n"
        f"<code>{referral_link}</code>\n\n"
        f"📊 <b>آمار فعالیت شما:</b>\n"
        f"• تعداد دوستان دعوت‌شده: <b>{invited_count} نفر</b>\n"
        f"• پاداش کسب‌شده: <b>{commission_irr // 10:,} تومان</b>\n\n"
        f"{referral_note}"
    )
    await message.answer(text, parse_mode="HTML")
