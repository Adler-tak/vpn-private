"""Wallet balance and top-up handlers."""
import uuid
import html
import secrets
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.user import User, Wallet
from app.database.models.payment import (
    Payment,
    PaymentReceipt,
    BankCard,
    PaymentGateway,
    get_active_bank_card_or_fallback,
    get_active_gateway_for_provider,
)
from app.integrations.payments.sep import SEPGateway
from app.integrations.payments.idpay import IDPayGateway
from app.integrations.payments.zarinpal import ZarinpalGateway
from app.bot.handlers.admin import generate_qr_buffer
from app.core.constants import PaymentMethod, PaymentStatus
from app.core.config import settings
from app.bot.states.customer import WalletTopupState
from app.bot.notifications import notify_all_admins
from app.bot.keyboards.inline import get_admin_payment_approval_keyboard, get_wallet_inline_keyboard
from app.bot.locales.fa import MAIN_MENU_BTNS
from app.modules.bot_texts.service import get_bot_text

router = Router()
logger = logging.getLogger(__name__)


def to_english_digits(text: str) -> str:
    """Convert Persian and Arabic numerals to English digits."""
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    for i in range(10):
        text = text.replace(persian_digits[i], str(i)).replace(arabic_digits[i], str(i))
    return text


@router.message(F.text == MAIN_MENU_BTNS["wallet"])
async def show_wallet_menu(message: Message, db_user: User, db_session: AsyncSession):
    wallet_res = await db_session.execute(select(Wallet).where(Wallet.user_id == db_user.id))
    wallet = wallet_res.scalar_one_or_none()
    balance_toman = wallet.balance_toman if wallet else 0
    balance_irr = wallet.balance_irr if wallet else 0

    guide_text = get_bot_text(
        "wallet_guide",
        "با شارژ کیف پول یا ثبت کارت‌های ووچر هدیه، می‌توانید بدون نیاز به کارت به کارت در هر زمان سرویس‌های خود را آنی خریداری و تمدید نمایید.",
    )
    text = (
        f"💳 <b>کیف پول حساب کاربری شما:</b>\n\n"
        f"💰 موجودی فعلی: <b>{balance_toman:,} تومان</b> ({balance_irr:,} ریال)\n\n"
        f"{guide_text}"
    )
    kb = get_wallet_inline_keyboard()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "wallet_view")
async def handle_wallet_view_callback(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    wallet_res = await db_session.execute(select(Wallet).where(Wallet.user_id == db_user.id))
    wallet = wallet_res.scalar_one_or_none()
    balance_toman = wallet.balance_toman if wallet else 0
    balance_irr = wallet.balance_irr if wallet else 0

    guide_text = get_bot_text(
        "wallet_guide",
        "با شارژ کیف پول یا ثبت کارت‌های ووچر هدیه، می‌توانید بدون نیاز به کارت به کارت در هر زمان سرویس‌های خود را آنی خریداری و تمدید نمایید.",
    )
    text = (
        f"💳 <b>کیف پول حساب کاربری شما:</b>\n\n"
        f"💰 موجودی فعلی: <b>{balance_toman:,} تومان</b> ({balance_irr:,} ریال)\n\n"
        f"{guide_text}"
    )
    kb = get_wallet_inline_keyboard()
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_(["wallet_topup_start", "wallet_topup"]))
async def handle_wallet_topup_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(WalletTopupState.entering_amount)
    min_amount = settings.MIN_WALLET_TOPUP_TOMAN

    text = (
        f"💳 <b>افزایش موجودی کیف پول:</b>\n\n"
        f"لطفاً یکی از مبالغ پیش‌فرض زیر را انتخاب کنید یا مبلغ مورد نظر خود را به <b>تومان</b> تایپ و ارسال فرمایید:\n\n"
        f"⚠️ <i>حداقل مبلغ قابل شارژ: <b>{min_amount:,} تومان</b></i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="۵۰,۰۰۰ تومان", callback_data="topup_amount:50000"),
            InlineKeyboardButton(text="۱۰۰,۰۰۰ تومان", callback_data="topup_amount:100000"),
        ],
        [
            InlineKeyboardButton(text="۲۰۰,۰۰۰ تومان", callback_data="topup_amount:200000"),
            InlineKeyboardButton(text="۵۰۰,۰۰۰ تومان", callback_data="topup_amount:500000"),
        ],
        [
            InlineKeyboardButton(text="🔙 انصراف و بازگشت", callback_data="topup_cancel"),
        ],
    ])
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "topup_cancel")
async def handle_topup_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("عملیات شارژ کیف پول لغو شد.")
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("عملیات لغو شد.")


@router.callback_query(F.data.startswith("topup_amount:"))
async def handle_topup_preset_amount(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    amount_str = callback.data.split(":")[1]
    try:
        amount_toman = int(amount_str)
    except ValueError:
        return await callback.answer("مبلغ نامعتبر است.", show_alert=True)

    min_amount = settings.MIN_WALLET_TOPUP_TOMAN
    if amount_toman < min_amount:
        return await callback.answer(f"حداقل مبلغ شارژ {min_amount:,} تومان است.", show_alert=True)

    await state.update_data(amount_toman=amount_toman)
    await state.set_state(WalletTopupState.selecting_method)
    await prompt_payment_method(callback.message, amount_toman, db_session=db_session)
    await callback.answer()


@router.message(WalletTopupState.entering_amount, F.text)
async def handle_topup_text_amount(message: Message, state: FSMContext, db_session: AsyncSession):
    raw_text = to_english_digits(message.text.strip().replace(",", "").replace("،", "").replace(" ", ""))
    if not raw_text.isdigit():
        return await message.answer(
            "⚠️ لطفاً مبلغ را تنها به صورت <b>عدد (به تومان)</b> ارسال کنید یا از گزینه‌های بالا انتخاب نمایید:",
            parse_mode="HTML",
        )

    amount_toman = int(raw_text)
    min_amount = settings.MIN_WALLET_TOPUP_TOMAN
    if amount_toman < min_amount:
        return await message.answer(
            f"⚠️ حداقل مبلغ شارژ کیف پول <b>{min_amount:,} تومان</b> می‌باشد.\nلطفاً مبلغی مساوی یا بیشتر وارد فرمایید:",
            parse_mode="HTML",
        )

    await state.update_data(amount_toman=amount_toman)
    await state.set_state(WalletTopupState.selecting_method)
    await prompt_payment_method(message, amount_toman, db_session=db_session)


async def prompt_payment_method(message: Message, amount_toman: int, db_session: AsyncSession = None):
    c2c_enabled = getattr(settings, "CARD_TO_CARD_ENABLED", False)
    crypto_enabled = getattr(settings, "CRYPTO_ENABLED", False)
    perfect_money_enabled = getattr(settings, "PERFECT_MONEY_ENABLED", False)
    zarinpal_enabled = getattr(settings, "ZARINPAL_ENABLED", False)
    sep_enabled = getattr(settings, "SEP_ENABLED", False)
    idpay_enabled = getattr(settings, "IDPAY_ENABLED", False)
    stripe_enabled = getattr(settings, "STRIPE_ENABLED", False)

    if db_session:
        try:
            gw_res = await db_session.execute(select(PaymentGateway.provider).where(PaymentGateway.is_active == True))
            for p in gw_res.scalars().all():
                p_lower = p.lower()
                if p_lower == "sep":
                    sep_enabled = True
                elif p_lower == "idpay":
                    idpay_enabled = True
                elif p_lower == "zarinpal":
                    zarinpal_enabled = True
                elif p_lower == "crypto":
                    crypto_enabled = True
                elif p_lower == "perfect_money":
                    perfect_money_enabled = True
                elif p_lower == "stripe":
                    stripe_enabled = True
        except Exception:
            pass

    methods = []
    if c2c_enabled:
        methods.append([InlineKeyboardButton(text="💳 کارت به کارت (کارت بانکی)", callback_data="topup_method:c2c")])
    if sep_enabled:
        methods.append([InlineKeyboardButton(text="🌐 درگاه پرداخت سامان (SEP)", callback_data="topup_method:sep")])
    if idpay_enabled:
        methods.append([InlineKeyboardButton(text="🌐 درگاه پرداخت آنلاین آیدی پی (IDPay)", callback_data="topup_method:idpay")])
    if zarinpal_enabled:
        methods.append([InlineKeyboardButton(text="🌐 درگاه پرداخت زرین‌پال", callback_data="topup_method:zarinpal")])
    if stripe_enabled:
        methods.append([InlineKeyboardButton(text="💳 درگاه بین‌المللی ارزی (Stripe)", callback_data="topup_method:stripe")])
    if crypto_enabled:
        methods.append([InlineKeyboardButton(text="🪙 تتر / رمزارز (USDT)", callback_data="topup_method:crypto")])
    if perfect_money_enabled:
        methods.append([InlineKeyboardButton(text="💵 پرفکت مانی (Perfect Money)", callback_data="topup_method:perfect_money")])

    methods.append([InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")])

    if not any([c2c_enabled, sep_enabled, idpay_enabled, zarinpal_enabled, stripe_enabled, crypto_enabled, perfect_money_enabled]):
        await message.answer(
            "⚠️ در حال حاضر هیچ درگاه پرداختی برای شارژ مستقیم فعال نیست. لطفاً با پشتیبانی در ارتباط باشید.",
            parse_mode="HTML",
        )
        return

    text = (
        f"💰 <b>مبلغ انتخابی:</b> <b>{amount_toman:,} تومان</b>\n\n"
        f"لطفاً روش پرداخت مورد نظر خود را جهت افزایش موجودی انتخاب فرمایید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=methods)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("topup_method:"))
async def handle_topup_method_selected(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    db_session: AsyncSession,
):
    method = callback.data.split(":")[1]
    data = await state.get_data()
    amount_toman = data.get("amount_toman")
    if not amount_toman:
        await state.clear()
        return await callback.answer("جلسه شما منقضی شده است. لطفاً مجدداً اقدام کنید.", show_alert=True)

    payment_id = uuid.uuid4()
    amount_irr = amount_toman * 10

    if method == "c2c":
        card_info = await get_active_bank_card_or_fallback(db_session)
        card_number = card_info["card_number"]
        card_bank_name = card_info["bank_name"]
        card_holder = card_info["card_holder"]

        tracking_code = f"C2C-{secrets.token_hex(4).upper()}"
        payment = Payment(
            id=payment_id,
            order_id=None,
            user_id=db_user.id,
            amount_irr=amount_irr,
            provider=PaymentMethod.CARD_TO_CARD,
            status=PaymentStatus.PENDING,
            tracking_code=tracking_code,
            admin_notes="WALLET_TOPUP",
        )
        db_session.add(payment)
        await db_session.commit()

        await state.update_data(payment_id=str(payment.id), method="c2c", card_number=card_number)
        await state.set_state(WalletTopupState.awaiting_c2c_receipt)

        text = (
            f"💳 <b>اطلاعات پرداخت کارت به کارت جهت افزایش موجودی:</b>\n\n"
            f"🏦 بانک: <b>{html.escape(card_bank_name)}</b>\n"
            f"🔢 شماره کارت (جهت کپی لمس نمایید):\n<code>{card_number}</code>\n"
            f"👤 به نام: <b>{html.escape(card_holder)}</b>\n"
            f"💰 مبلغ قابل انتقال: <b>{amount_toman:,} تومان</b>\n"
            f"🔖 شناسه پیگیری سیستم: <code>{payment.tracking_code}</code>\n\n"
            f"📸 لطفاً پس از واریز، <b>تصویر فیش واریزی</b> خود را در همین بخش ارسال نمایید:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 کپی شماره کارت", callback_data=f"copy_card:{card_number}")],
            [InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")]
        ])
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer(f"💳 شماره کارت: {card_number}\nبانک {card_bank_name} ({card_holder})", show_alert=True)

    elif method == "sep":
        gw = await get_active_gateway_for_provider(db_session, "sep")
        tracking_code = f"SEP-{secrets.token_hex(4).upper()}"
        payment = Payment(
            id=payment_id,
            order_id=None,
            user_id=db_user.id,
            amount_irr=amount_irr,
            provider=PaymentMethod.SEP,
            gateway_id=gw.id if gw else None,
            status=PaymentStatus.PENDING,
            tracking_code=tracking_code,
            admin_notes="WALLET_TOPUP",
        )
        db_session.add(payment)
        await db_session.commit()

        if gw:
            cfg = gw.config_dict
            sep_gw = SEPGateway(terminal_id=cfg.get("terminal_id"), merchant_id=cfg.get("merchant_id"))
        else:
            sep_gw = SEPGateway()

        init_res = await sep_gw.request_payment(
            payment_id=str(payment.id),
            amount_irr=amount_irr,
            description="شارژ کیف پول VPN Private",
            metadata={"tracking_code": tracking_code, "phone": getattr(db_user, "phone", "")},
        )
        if init_res.authority:
            payment.admin_notes = f"TOKEN:{init_res.authority}"
            payment.reference_id = init_res.authority
            await db_session.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ورود به درگاه سامان شاپرک (SEP)", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")]
        ])
        await callback.message.answer(
            f"💳 <b>افزایش موجودی کیف پول - درگاه سامان شاپرک</b>\n\n"
            f"💰 مبلغ شارژ: <b>{amount_toman:,} تومان</b>\n"
            f"🔖 شناسه پیگیری سیستم: <code>{tracking_code}</code>\n\n"
            f"جهت پرداخت امن، روی دکمه زیر کلیک نمایید. پس از واریز، موجودی کیف پول شما به صورت خودکار و آنی افزایش می‌یابد.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await state.clear()
        await callback.answer()
        return

    elif method == "idpay":
        gw = await get_active_gateway_for_provider(db_session, "idpay")
        tracking_code = f"IDP-{secrets.token_hex(4).upper()}"
        payment = Payment(
            id=payment_id,
            order_id=None,
            user_id=db_user.id,
            amount_irr=amount_irr,
            provider=PaymentMethod.IDPAY,
            gateway_id=gw.id if gw else None,
            status=PaymentStatus.PENDING,
            tracking_code=tracking_code,
            admin_notes="WALLET_TOPUP",
        )
        db_session.add(payment)
        await db_session.commit()

        if gw:
            cfg = gw.config_dict
            idp_gw = IDPayGateway(api_key=cfg.get("api_key"), sandbox=cfg.get("sandbox", False))
        else:
            idp_gw = IDPayGateway()

        init_res = await idp_gw.request_payment(
            payment_id=str(payment.id),
            amount_irr=amount_irr,
            description="شارژ کیف پول VPN Private",
            metadata={"tracking_code": tracking_code, "name": db_user.first_name or "کاربر", "phone": getattr(db_user, "phone", "")},
        )
        if init_res.authority:
            payment.reference_id = init_res.authority
            await db_session.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ورود به درگاه آیدی پی (IDPay)", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")]
        ])
        await callback.message.answer(
            f"💳 <b>افزایش موجودی کیف پول - درگاه آیدی پی</b>\n\n"
            f"💰 مبلغ شارژ: <b>{amount_toman:,} تومان</b>\n"
            f"🔖 شناسه پیگیری سیستم: <code>{tracking_code}</code>\n\n"
            f"جهت پرداخت امن، روی دکمه زیر کلیک نمایید. پس از واریز، موجودی کیف پول شما به صورت خودکار و آنی افزایش می‌یابد.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await state.clear()
        await callback.answer()
        return

    elif method == "zarinpal":
        gw = await get_active_gateway_for_provider(db_session, "zarinpal")
        payment = Payment(
            id=payment_id,
            order_id=None,
            user_id=db_user.id,
            amount_irr=amount_irr,
            provider=PaymentMethod.ZARINPAL,
            gateway_id=gw.id if gw else None,
            status=PaymentStatus.PENDING,
            admin_notes="WALLET_TOPUP",
        )
        db_session.add(payment)
        await db_session.commit()

        if gw:
            cfg = gw.config_dict
            z_gw = ZarinpalGateway(merchant_id=cfg.get("merchant_id"), sandbox=cfg.get("sandbox", False))
        else:
            z_gw = ZarinpalGateway()

        init_res = await z_gw.request_payment(
            payment_id=str(payment.id),
            amount_irr=amount_irr,
            description="شارژ کیف پول VPN Private",
        )
        payment.tracking_code = init_res.authority
        await db_session.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ورود به صفحه پرداخت زرین‌پال", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")]
        ])
        await callback.message.answer(
            f"💳 <b>افزایش موجودی کیف پول - درگاه زرین‌پال</b>\n\n"
            f"💰 مبلغ شارژ: <b>{amount_toman:,} تومان</b>\n"
            f"🔖 شناسه پیگیری سیستم: <code>{payment.tracking_code}</code>\n\n"
            f"جهت تکمیل پرداخت مبلغ بر روی دکمه زیر کلیک کنید. پس از پرداخت آنلاین، کیف پول شما بلافاصله شارژ خواهد شد.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()
        await callback.answer()
        return

    elif method == "stripe":
        from app.integrations.payments.stripe_gw import StripeGateway
        gw = await get_active_gateway_for_provider(db_session, "stripe")
        tracking_code = f"STRP-{secrets.token_hex(4).upper()}"
        payment = Payment(
            id=payment_id,
            order_id=None,
            user_id=db_user.id,
            amount_irr=amount_irr,
            provider=PaymentMethod.STRIPE,
            gateway_id=gw.id if gw else None,
            status=PaymentStatus.PENDING,
            tracking_code=tracking_code,
            admin_notes="WALLET_TOPUP",
        )
        db_session.add(payment)
        await db_session.commit()

        if gw:
            cfg = gw.config_dict
            s_gw = StripeGateway(
                secret_key=cfg.get("secret_key"),
                publishable_key=cfg.get("publishable_key"),
                currency=cfg.get("currency", "USD"),
                exchange_rate_toman=int(cfg.get("exchange_rate", 70000) or 70000),
            )
        else:
            s_gw = StripeGateway()

        init_res = await s_gw.request_payment(
            payment_id=str(payment.id),
            amount_irr=amount_irr,
            description="VPN Private Wallet Top-Up",
            metadata={"tracking_code": tracking_code},
        )
        if init_res.status == "FAILED" or not init_res.redirect_url:
            await callback.message.answer(
                f"⚠️ {init_res.instructions or 'خطا در اتصال به درگاه استرایپ.'}",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        if init_res.authority:
            payment.reference_id = init_res.authority
            await db_session.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 ورود به درگاه بین‌المللی استرایپ (Stripe)", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")]
        ])
        await callback.message.answer(
            f"💳 <b>افزایش موجودی کیف پول - درگاه بین‌المللی استرایپ (Stripe)</b>\n\n"
            f"💰 مبلغ شارژ: <b>{amount_toman:,} تومان</b>\n"
            f"🔖 شناسه پیگیری سیستم: <code>{payment.tracking_code}</code>\n\n"
            f"جهت پرداخت ارزی با کارت‌های بانکی بین‌المللی (Visa / Mastercard / Apple Pay) بر روی دکمه زیر کلیک کنید. پس از پرداخت، کیف پول شما بلافاصله شارژ خواهد شد.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()
        await callback.answer()
        return

    elif method == "perfect_money":
        from app.integrations.payments.perfect_money import PerfectMoneyGateway
        gw = await get_active_gateway_for_provider(db_session, "perfect_money")
        payment = Payment(
            id=payment_id,
            order_id=None,
            user_id=db_user.id,
            amount_irr=amount_irr,
            provider=PaymentMethod.PERFECT_MONEY,
            gateway_id=gw.id if gw else None,
            status=PaymentStatus.PENDING,
            admin_notes="WALLET_TOPUP",
        )
        db_session.add(payment)
        await db_session.commit()

        if gw:
            cfg = gw.config_dict
            pm_gw = PerfectMoneyGateway(
                account_id=cfg.get("account_id"),
                passphrase=cfg.get("passphrase"),
                usd_rate=int(cfg.get("usd_rate", 70000)),
            )
        else:
            pm_gw = PerfectMoneyGateway()

        init_res = await pm_gw.request_payment(
            payment_id=str(payment.id),
            amount_irr=amount_irr,
            description="شارژ کیف پول VPN Private",
        )
        payment.tracking_code = init_res.tracking_code
        await db_session.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ورود به درگاه پرفکت مانی (Perfect Money)", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")]
        ])
        rate = pm_gw.usd_rate
        usd_amount = pm_gw.calculate_usd_amount(amount_irr)
        await callback.message.answer(
            f"💵 <b>افزایش موجودی کیف پول - پرفکت مانی</b>\n\n"
            f"💰 مبلغ دلاری: <b>{usd_amount:.2f} USD</b>\n"
            f"💵 معادل تومانی: <b>{amount_toman:,} تومان</b> (نرخ هر دلار: {rate:,} تومان)\n"
            f"📍 شماره حساب مقصد: <code>{pm_gw.account_id}</code>\n"
            f"🔖 شناسه پیگیری سیستم: <code>{payment.tracking_code}</code>\n\n"
            f"جهت پرداخت روی دکمه زیر کلیک نمایید. پس از واریز، موجودی کیف پول شما به صورت خودکار و آنی افزایش می‌یابد.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()
        await callback.answer()
        return

    elif method == "crypto":
        trc20_enabled = getattr(settings, "CRYPTO_TRC20_ENABLED", True)
        bep20_enabled = getattr(settings, "CRYPTO_BEP20_ENABLED", False)

        if trc20_enabled and bep20_enabled:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="⚡️ TRC-20 (Tron)", callback_data="topup_crypto_net:trc20"),
                    InlineKeyboardButton(text="🟡 BEP-20 (BSC)", callback_data="topup_crypto_net:bep20"),
                ],
                [InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")]
            ])
            text = (
                f"🪙 <b>انتخاب شبکه انتقال تتر (USDT):</b>\n\n"
                f"💰 مبلغ شارژ کیف پول: <b>{amount_toman:,} تومان</b>\n\n"
                f"لطفاً شبکه انتقال تتر مورد نظر خود را انتخاب نمایید:\n"
                f"• <b>TRC-20 (Tron):</b> شبکه ترون\n"
                f"• <b>BEP-20 (BSC):</b> شبکه هوشمند بایننس (Binance Smart Chain)"
            )
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()
            return
        else:
            network = "bep20" if (bep20_enabled and not trc20_enabled) else "trc20"
            await show_topup_crypto_instructions(
                target_msg=callback.message,
                state=state,
                db_user=db_user,
                db_session=db_session,
                amount_toman=amount_toman,
                network=network,
            )
            await callback.answer()
            return


async def show_topup_crypto_instructions(
    target_msg: Message,
    state: FSMContext,
    db_user: User,
    db_session: AsyncSession,
    amount_toman: int,
    network: str,
):
    payment_id = uuid.uuid4()
    amount_irr = amount_toman * 10
    rate = getattr(settings, "USDT_TOMAN_RATE", 70000)
    usdt_amount = round(amount_toman / rate, 2)
    if usdt_amount < 1.0:
        usdt_amount = 1.0

    tracking_code = f"CRYPTO-{payment_id.hex[:6].upper()}"
    payment = Payment(
        id=payment_id,
        order_id=None,
        user_id=db_user.id,
        amount_irr=amount_irr,
        provider=PaymentMethod.CRYPTO,
        status=PaymentStatus.PENDING,
        tracking_code=tracking_code,
        admin_notes="WALLET_TOPUP",
    )
    db_session.add(payment)
    await db_session.commit()

    await state.update_data(payment_id=str(payment.id), method="crypto", crypto_network=network)
    await state.set_state(WalletTopupState.awaiting_c2c_receipt)

    if network.lower() in ("bep20", "bsc", "bep-20"):
        net_title = "BEP-20 (BSC)"
        wallet_addr = settings.CRYPTO_WALLET_BEP20 or "0x587EF4ea392c6947688533fb0550Bae94B8f0E8a"
        net_alert = "⚠️ <b>توجه مهم:</b> لطفاً انتقال را دقیقاً بر روی شبکه <b>BEP-20 (Binance Smart Chain / BSC)</b> انجام دهید."
    else:
        net_title = "TRC-20 (Tron)"
        wallet_addr = settings.CRYPTO_WALLET_TRC20 or "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
        net_alert = "⚠️ <b>توجه مهم:</b> لطفاً انتقال را دقیقاً بر روی شبکه <b>TRC-20 (Tron)</b> انجام دهید."

    qr_buf = generate_qr_buffer(wallet_addr)
    qr_file = BufferedInputFile(qr_buf.getvalue(), filename=f"usdt_{network.lower()}_qr.png")

    text = (
        f"🪙 <b>اطلاعات پرداخت با ارز دیجیتال (تتر USDT - شبکه {net_title}):</b>\n\n"
        f"💰 مبلغ دلاری: <b>{usdt_amount} USDT</b>\n"
        f"💵 معادل تومانی: <b>{amount_toman:,} تومان</b> (نرخ هر تتر: {rate:,} تومان)\n"
        f"🔖 شناسه پیگیری سیستم: <code>{payment.tracking_code}</code>\n\n"
        f"📍 <b>آدرس کیف پول تتر ({net_title}):</b>\n<code>{wallet_addr}</code>\n\n"
        f"{net_alert}\n\n"
        f"📸 پس از پرداخت، لطفاً <b>تصویر رسید یا کد هش (TXID)</b> تراکنش را در همین بخش ارسال نمایید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 کپی آدرس کیف پول", callback_data=f"copy_crypto:{wallet_addr}")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data="topup_cancel")]
    ])
    try:
        await target_msg.answer_photo(photo=qr_file, caption=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await target_msg.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("topup_crypto_net:"))
async def handle_topup_crypto_network(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    db_session: AsyncSession,
):
    network = callback.data.split(":")[1]
    data = await state.get_data()
    amount_toman = data.get("amount_toman")
    if not amount_toman:
        await state.clear()
        return await callback.answer("جلسه شما منقضی شده است. لطفاً مجدداً اقدام کنید.", show_alert=True)

    await show_topup_crypto_instructions(
        target_msg=callback.message,
        state=state,
        db_user=db_user,
        db_session=db_session,
        amount_toman=amount_toman,
        network=network,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("copy_card:"))
async def handle_copy_card(callback: CallbackQuery):
    card = callback.data.split(":", 1)[1]
    await callback.answer(f"📋 شماره کارت:\n{card}\n\nجهت واریز مبلغ در اپلیکیشن بانکی آماده است.", show_alert=True)


@router.callback_query(F.data.startswith("copy_crypto:"))
async def handle_copy_crypto(callback: CallbackQuery):
    addr = callback.data.split(":", 1)[1]
    await callback.answer(f"📍 آدرس کیف پول:\n{addr}\n\nجهت انتقال کپی شد.", show_alert=True)


@router.message(
    WalletTopupState.awaiting_c2c_receipt,
    F.photo | F.document | (F.text & ~F.text.startswith("/"))
)
async def handle_topup_receipt_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    data = await state.get_data()
    payment_id_str = data.get("payment_id")
    payment = None
    if payment_id_str:
        try:
            payment = await db_session.get(Payment, uuid.UUID(payment_id_str))
        except Exception:
            pass

    if not payment:
        pay_res = await db_session.execute(
            select(Payment)
            .where(
                Payment.user_id == db_user.id,
                Payment.order_id.is_(None),
                Payment.status == PaymentStatus.PENDING,
            )
            .order_by(Payment.created_at.desc())
        )
        payment = pay_res.scalars().first()

    if not payment:
        await state.clear()
        return await message.answer(
            "⚠️ درخواست شارژ معتبری یافت نشد. لطفاً مجدداً از منوی کیف پول اقدام فرمایید.",
            parse_mode="HTML",
        )

    is_text_txid = False
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        doc = message.document
        if doc.file_size and doc.file_size > 15 * 1024 * 1024:
            return await message.answer("⚠️ حجم فایل ارسالی بیش از حد مجاز (۱۵ مگابایت) است.")
        file_id = doc.file_id
    elif message.text:
        is_text_txid = True
        file_id = f"TXID:{message.text.strip()}"
    else:
        return await message.answer("⚠️ لطفاً تصویر رسید یا کد رهگیری را ارسال فرمایید.")

    receipt_res = await db_session.execute(
        select(PaymentReceipt).where(PaymentReceipt.payment_id == payment.id)
    )
    receipt = receipt_res.scalar_one_or_none()

    if receipt:
        receipt.telegram_file_id = file_id
        receipt.submitted_amount_toman = payment.amount_toman
    else:
        receipt = PaymentReceipt(
            id=uuid.uuid4(),
            payment_id=payment.id,
            telegram_file_id=file_id,
            submitted_amount_toman=payment.amount_toman,
        )
        db_session.add(receipt)
    await db_session.commit()
    await state.clear()

    method_fa = "ارز دیجیتال (تتر TRC-20)" if payment.provider == PaymentMethod.CRYPTO else "کارت به کارت"
    await message.answer(
        f"✅ <b>رسید افزایش موجودی ({method_fa}) شما با موفقیت ثبت شد.</b>\n\n"
        f"💰 <b>مبلغ فیش:</b> <b>{payment.amount_toman:,} تومان</b>\n"
        f"🔖 <b>شناسه پیگیری:</b> <code>{payment.tracking_code}</code>\n\n"
        f"⏳ رسید جهت بررسی و تایید برای مدیریت ارسال گردید. به محض تایید، موجودی کیف پول شما افزایش خواهد یافت.",
        parse_mode="HTML",
    )

    # Broadcast approval card to all admins
    u_username = f"@{html.escape(db_user.username)}" if db_user.username else "ندارد"
    u_name = html.escape(db_user.first_name or "کاربر")
    admin_caption = (
        f"🔔 <b>درخواست شارژ کیف پول جدید ({html.escape(method_fa)})</b>\n\n"
        f"👤 <b>کاربر:</b> {u_name} ({u_username})\n"
        f"🆔 <b>شناسه کاربر:</b> <code>{db_user.telegram_id}</code>\n"
        f"💰 <b>مبلغ شارژ:</b> <b>{payment.amount_toman:,} تومان</b>\n"
        f"🔖 <b>کد پیگیری:</b> <code>{payment.tracking_code}</code>\n"
    )
    if is_text_txid:
        admin_caption += f"🔗 <b>کد رهگیری / TXID:</b>\n<code>{html.escape(message.text.strip())}</code>\n"

    approval_kb = get_admin_payment_approval_keyboard(str(payment.id))
    await notify_all_admins(
        bot=bot,
        db_session=db_session,
        text=admin_caption,
        photo_file_id=file_id if message.photo else None,
        document_file_id=file_id if message.document else None,
        reply_markup=approval_kb,
    )
    logger.info(f"Broadcasted wallet top-up receipt {payment.tracking_code} to all admins.")
