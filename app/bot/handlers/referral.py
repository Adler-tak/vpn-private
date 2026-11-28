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


DEFAULT_REFERRAL_TEMPLATE = (
    "👥 <b>سیستم همکاری در فروش و دعوت از دوستان:</b>\n\n"
    "با دعوت از دوستان خود به ربات، <b>{commission_percent}٪</b> از هر خرید آن‌ها به صورت خودکار به کیف پول شما واریز می‌شود!\n\n"
    "🔗 <b>لینک دعوت اختصاصی شما:</b>\n"
    "<code>{referral_link}</code>\n\n"
    "📊 <b>آمار فعالیت شما:</b>\n"
    "• تعداد دوستان دعوت‌شده: <b>{invited_count} نفر</b>\n"
    "• پاداش کسب‌شده: <b>{commission_toman} تومان</b>\n\n"
    "💡 <i>پاداش‌های شما در کیف پول ذخیره شده و می‌توانید با آن سرویس رایگان تهیه فرمایید یا موجودی خود را افزایش دهید.</i>"
)


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

    commission_percent = getattr(settings, "REFERRAL_COMMISSION_PERCENT", 10)
    commission_toman = f"{commission_irr // 10:,}"

    raw_template = get_bot_text("referral_guide", DEFAULT_REFERRAL_TEMPLATE)
    
    # Backward compatibility: if an admin had saved only the old single-line footnote without template variables:
    if raw_template and "{referral_link}" not in raw_template and "{invited_count}" not in raw_template and "👥" not in raw_template:
        text = (
            f"👥 <b>سیستم همکاری در فروش و دعوت از دوستان:</b>\n\n"
            f"با دعوت از دوستان خود به ربات، <b>{commission_percent}٪</b> از هر خرید آن‌ها به صورت خودکار به کیف پول شما واریز می‌شود!\n\n"
            f"🔗 <b>لینک دعوت اختصاصی شما:</b>\n"
            f"<code>{referral_link}</code>\n\n"
            f"📊 <b>آمار فعالیت شما:</b>\n"
            f"• تعداد دوستان دعوت‌شده: <b>{invited_count} نفر</b>\n"
            f"• پاداش کسب‌شده: <b>{commission_toman} تومان</b>\n\n"
            f"{raw_template}"
        )
    else:
        text = (
            raw_template
            .replace("{referral_link}", referral_link)
            .replace("{commission_percent}", str(commission_percent))
            .replace("{invited_count}", str(invited_count))
            .replace("{commission_toman}", commission_toman)
            .replace("{bot_username}", bot_username)
        )

    await message.answer(text, parse_mode="HTML")
