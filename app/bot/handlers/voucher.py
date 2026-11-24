"""Telegram Bot Voucher & Scratch Card Redemption Handlers."""
import logging
import uuid
from typing import Optional
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.user import User
from app.modules.vouchers.service import VoucherService
from app.core.config import settings
from app.bot.states.customer import VoucherRedeemState
from app.bot.keyboards.inline import (
    get_voucher_cancel_keyboard,
    get_voucher_success_keyboard,
)
from app.bot.locales.fa import MAIN_MENU_BTNS
from app.bot.handlers.admin import generate_qr_buffer

logger = logging.getLogger(__name__)
router = Router()


async def process_voucher_redemption(
    target_message: Message,
    user: User,
    code: str,
    db_session: AsyncSession,
    bot: Optional[Bot] = None,
):
    """Core executor for voucher redemption from deep-link or manual entry."""
    raw_code = (code or "").strip()
    if not raw_code:
        await target_message.answer("❌ اطلاعات وارد شده خالی است.")
        return

    wait_msg = await target_message.answer("⏳ در حال بررسی و اعتبارسنجی کارت ووچر...")
    voucher_service = VoucherService(db_session)

    try:
        res = await voucher_service.redeem_voucher(raw_code=raw_code, user=user)

        try:
            await wait_msg.delete()
        except Exception:
            pass

        if not res.get("success"):
            err_msg = res.get("message", "❌ خطا در ثبت کارت ووچر.")
            await target_message.answer(err_msg, parse_mode="HTML")
            return

        vtype = res.get("type")

        # Record AuditLog and send Telegram alert to Admins
        try:
            from datetime import datetime, timezone
            from app.database.models.audit import AuditLog
            from app.bot.notifications import notify_all_admins

            voucher_obj = res.get("voucher")
            v_code = getattr(voucher_obj, "code", raw_code)
            traffic_gb = getattr(voucher_obj, "traffic_gb", 0)
            duration_days = getattr(voucher_obj, "duration_days", 0)
            amount_toman = res.get("amount_toman") or getattr(voucher_obj, "amount_toman", 0)

            if vtype == "wallet_credit":
                card_desc = f"شارژ کیف پول ({amount_toman:,} تومان)"
            else:
                card_desc = f"اشتراک {traffic_gb} گیگابایت / {duration_days} روزه"

            audit_entry = AuditLog(
                actor_id=user.telegram_id,
                actor_role="CUSTOMER",
                action="VOUCHER_REDEEMED",
                target_type="Voucher",
                target_id=str(v_code),
                details=f"مصرف ووچر کارت «{v_code}» ({card_desc}) توسط {user.first_name or ''} {user.last_name or ''} (@{user.username or 'ندارد'})",
            )
            db_session.add(audit_entry)
            await db_session.flush()

            effective_bot = bot or target_message.bot
            if effective_bot:
                full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "کاربر تلگرام"
                uname = f"@{user.username}" if user.username else "ندارد"
                admin_alert = (
                    f"🎟 <b>ووچر کد در ربات مصرف شد!</b>\n\n"
                    f"▫️ <b>کد ووچر:</b> <code>{v_code}</code>\n"
                    f"▫️ <b>مشخصات کارت:</b> {card_desc}\n"
                    f"▫️ <b>مصرف‌کننده:</b> {full_name} ({uname})\n"
                    f"▫️ <b>شناسه عددی (Chat ID):</b> <code>{user.telegram_id}</code>\n"
                    f"▫️ <b>زمان مصرف:</b> <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</code>"
                )
                await notify_all_admins(bot=effective_bot, db_session=db_session, text=admin_alert)
        except Exception as e_v_alert:
            logger.error(f"Error recording audit or alerting admins for voucher: {e_v_alert}")

        # 1. Wallet Credit Card
        if vtype == "wallet_credit":
            msg_text = res.get("message")
            kb = get_voucher_success_keyboard(voucher_type="wallet_credit")
            await target_message.answer(msg_text, reply_markup=kb, parse_mode="HTML")

        # 2. Plan Subscription Card
        elif vtype == "plan":
            conn = res.get("conn") or {}
            sub = res.get("subscription")
            msg_text = res.get("message")
            sub_id_str = str(sub.id) if sub else ""
            kb = get_voucher_success_keyboard(voucher_type="plan", sub_id=sub_id_str)

            sub_url = conn.get("subscription_url")
            if sub_url:
                try:
                    qr_buf = generate_qr_buffer(sub_url)
                    qr_file = BufferedInputFile(qr_buf.getvalue(), filename="vpn_voucher_sub.png")
                    await target_message.answer_photo(
                        photo=qr_file,
                        caption=msg_text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                    return
                except Exception as qr_err:
                    logger.warning(f"Failed to generate QR buffer for voucher: {qr_err}")

            await target_message.answer(msg_text, reply_markup=kb, parse_mode="HTML")

    except Exception as exc:
        logger.error(f"Unexpected error in voucher redemption: {exc}", exc_info=True)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await target_message.answer(f"❌ خطای سیستمی در پردازش ووچر: {exc}")


VOUCHER_PROMPT_TEXT = (
    "🎁 <b>ثبت کارت ووچر یا اکانت اشتراک:</b>\n\n"
    "لطفاً <b>کد کارت ووچر</b> خود (مانند <code>VPN-XXXX-XXXX-XXXX</code>) یا <b>نام کاربری و رمز عبور</b> را وارد فرمایید:\n\n"
    "📝 <b>فرمت ورود با نام کاربری و رمز عبور:</b>\n"
    "<code>یوزرنیم پسورد</code> (با یک فاصله بین نام کاربری و پسورد)\n\n"
    "💡 <i>مثال: <code>user123 pass456</code></i>"
)


@router.callback_query(F.data == "voucher_redeem_start")
async def handle_voucher_redeem_start(callback: CallbackQuery, state: FSMContext):
    """Prompt user to type their scratch voucher code or legacy username/password."""
    await state.set_state(VoucherRedeemState.entering_code)
    await callback.message.answer(
        VOUCHER_PROMPT_TEXT,
        reply_markup=get_voucher_cancel_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Command("voucher"))
@router.message(Command("redeem"))
@router.message(F.text.in_([
    MAIN_MENU_BTNS.get("voucher", "🎁 ثبت کارت ووچر"),
    "🎁 ثبت کارت ووچر",
    "🎁 ثبت کارت ووچر (کارت هدیه)",
    "🎁 ثبت ووچر / کارت هدیه",
    "ثبت کارت ووچر",
    "کارت هدیه",
    "ووچر",
]))
async def handle_voucher_command(message: Message, state: FSMContext):
    """Handle text command or button to redeem a voucher."""
    await state.set_state(VoucherRedeemState.entering_code)
    await message.answer(
        VOUCHER_PROMPT_TEXT,
        reply_markup=get_voucher_cancel_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "voucher_cancel")
async def handle_voucher_cancel(callback: CallbackQuery, state: FSMContext):
    """Cancel voucher input and clear state."""
    await state.clear()
    await callback.answer("عملیات ثبت کارت ووچر لغو شد.")
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("عملیات ثبت ووچر لغو شد.")


@router.message(VoucherRedeemState.entering_code)
async def handle_voucher_code_input(
    message: Message,
    state: FSMContext,
    db_user: User,
    db_session: AsyncSession,
    bot: Bot,
):
    """Receive and process entered voucher code or username/password from customer."""
    code_text = (message.text or "").strip()
    await state.clear()

    if not code_text or len(code_text) < 2:
        return await message.answer(
            "❌ اطلاعات وارد شده کوتاه است. لطفاً کد ووچر یا نام کاربری و رمز عبور را ارسال فرمایید."
        )

    await process_voucher_redemption(
        target_message=message,
        user=db_user,
        code=code_text,
        db_session=db_session,
        bot=bot,
    )


# =========================================================================
# 1. UNBOX GIFT CARD CALLBACK
# =========================================================================

@router.callback_query(F.data.startswith("open_gift:"))
async def handle_open_gift_callback(
    callback: CallbackQuery,
    db_user: User,
    db_session: AsyncSession,
    bot: Bot,
):
    """Unbox and instantly redeem a gifted subscription via Telegram inline button."""
    code = callback.data.split(":", 1)[1]
    await callback.answer("🎁 در حال فعال‌سازی هدیه شما...")
    await process_voucher_redemption(
        target_message=callback.message,
        user=db_user,
        code=code,
        db_session=db_session,
        bot=bot,
    )


# =========================================================================
# 2. DAILY LUCKY WHEEL BOT HANDLERS
# =========================================================================

@router.message(Command("spin"))
@router.message(F.text.in_([
    MAIN_MENU_BTNS.get("lucky_wheel", "🎡 گردونه شانس روزانه"),
    "🎡 گردونه شانس روزانه",
    "گردونه شانس",
    "گردونه",
]))
async def handle_lucky_wheel_command(
    message: Message,
    db_user: User,
    db_session: AsyncSession,
):
    """Display user's lucky wheel eligibility and allow instant spin."""
    from app.services.lucky_wheel_service import LuckyWheelService

    status = await LuckyWheelService.get_user_status(db_user.id, db_session)
    if not status.get("is_enabled"):
        return await message.answer("⚠️ گردونه شانس در حال حاضر موقتاً غیرفعال است.")

    prizes_desc = "\n".join([
        f"• {p.get('icon', '🎁')} <b>{p.get('label')}</b>"
        for p in status.get("prizes", [])
    ])

    if status.get("can_spin"):
        text = (
            "🎡 <b>گردونه شانس و جوایز روزانه VPN Private</b> 🎁\n\n"
            "هر ۲۴ ساعت یک شانس رایگان برای برنده شدن ترافیک، شارژ کیف پول و تخفیف‌های شگفت‌انگیز دارید!\n\n"
            f"<b>جوایز موجود در گردونه:</b>\n{prizes_desc}\n\n"
            "هم‌اکنون روی دکمه زیر کلیک کنید تا گردونه برای شما بچرخد! 🍀"
        )
        inline_buttons = [
            [InlineKeyboardButton(text="🎲 چرخاندن گردونه شانس!", callback_data="spin_lucky_wheel_action")],
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=inline_buttons)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        rem = status.get("remaining_seconds", 0)
        hours = rem // 3600
        mins = (rem % 3600) // 60
        text = (
            "⏳ <b>شما شانس امروز خود را استفاده کرده‌اید!</b>\n\n"
            f"زمان باقیمانده تا شانس بعدی شما: <b>{hours} ساعت و {mins} دقیقه</b>\n\n"
            "فردا مجدداً سر بزنید و شانس خود را امتحان کنید! 🌟"
        )
        await message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "spin_lucky_wheel_action")
async def handle_spin_wheel_action(
    callback: CallbackQuery,
    db_user: User,
    db_session: AsyncSession,
):
    """Execute wheel spin and announce reward."""
    from app.services.lucky_wheel_service import LuckyWheelService

    res = await LuckyWheelService.spin(user=db_user, db=db_session)
    if not res.get("success"):
        return await callback.answer(res.get("message", "خطا در چرخاندن گردونه."), show_alert=True)

    await callback.answer("🎲 گردونه با موفقیت چرخید!")
    msg = res.get("message")
    await callback.message.answer(
        f"🎰 <b>نتیجه چرخاندن گردونه شانس:</b>\n\n{msg}",
        parse_mode="HTML",
    )


# =========================================================================
# 3. TELEGRAM GIFT SUBSCRIPTION PURCHASE BOT HANDLERS
# =========================================================================

@router.message(Command("gift"))
@router.message(F.text.in_([
    MAIN_MENU_BTNS.get("gift_card", "🎁 کارت هدیه تلگرامی"),
    "🎁 کارت هدیه تلگرامی",
    "کارت هدیه",
    "خرید هدیه",
]))
async def handle_gift_card_menu(
    message: Message,
    db_user: User,
    db_session: AsyncSession,
):
    """Display available packages to purchase as a gift card."""
    from app.database.models.product import Product

    products = (await db_session.execute(
        select(Product)
        .where(Product.is_active == True)
        .order_by(Product.sort_order.asc(), Product.price_irr.asc())
    )).scalars().all()

    if not products:
        return await message.answer("❌ در حال حاضر بسته‌ای برای خرید کارت هدیه تعریف نشده است.")

    buttons = []
    for p in products[:6]:
        buttons.append([
            InlineKeyboardButton(
                text=f"🎁 {p.name} ({p.traffic_gb}GB - {p.duration_days} روز) | {p.price_toman:,} تومان",
                callback_data=f"buy_gift_prod:{p.id}",
            )
        ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = (
        "🎁 <b>سامانه صدور کارت هدیه تلگرامی VPN Private</b> ✨\n\n"
        "با خرید کارت هدیه، یک لینک و پیام شیک صادر می‌شود که می‌توانید آن را برای دوستان خود فوروارد کنید.\n"
        "دوست شما با یک کلیک روی لینک، اشتراک خود را دریافت و متصل می‌شود!\n\n"
        "لطفاً بسته مد نظر خود را جهت هدیه دادن انتخاب فرمایید:\n"
        "<i>(هزینه از موجودی کیف پول شما کسر خواهد شد)</i>"
    )
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("buy_gift_prod:"))
async def handle_buy_gift_product(
    callback: CallbackQuery,
    db_user: User,
    db_session: AsyncSession,
):
    """Purchase gift card and return shareable link."""
    p_id_str = callback.data.split(":", 1)[1]
    try:
        p_uuid = uuid.UUID(p_id_str)
    except ValueError:
        return await callback.answer("بسته نامعتبر است.", show_alert=True)

    from app.services.gift_card_service import GiftCardService

    res = await GiftCardService.create_gift_card(
        user=db_user,
        product_id=p_uuid,
        sender_name=db_user.first_name or "یک دوست عزیز",
        gift_note="یک هدیه ویژه اینترنت پرسرعت برای تو رفیق! 🎁✨",
        db=db_session,
    )

    if not res.get("success"):
        if res.get("needs_topup"):
            diff = res.get("shortage_toman", 0)
            return await callback.answer(f"❌ موجودی کیف پول کافی نیست. کسری: {diff:,} تومان", show_alert=True)
        return await callback.answer(res.get("message", "خطا در ساخت کارت هدیه"), show_alert=True)

    await callback.answer("🎉 کارت هدیه با موفقیت صادر شد!")
    share_text = res.get("share_text")
    deep_link = res.get("deep_link")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🚀 ارسال به دوستان در تلگرام (Share)",
            url=f"https://t.me/share/url?url={deep_link}&text={res.get('product_name')}",
        )
    ]])
    await callback.message.answer(
        f"🎉 <b>کارت هدیه شما آماده شد!</b>\n\n"
        f"پیام زیر را برای دوست خود فوروارد کنید یا لینک را برایش بفرستید:\n\n"
        f"{share_text}",
        reply_markup=kb,
    )

