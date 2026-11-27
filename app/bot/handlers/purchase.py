"""Customer purchase and payment workflows."""
import io
import uuid
import logging
import qrcode
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User, Wallet
from app.database.models.product import Product
from app.database.models.panel import Server
from app.database.models.order import Order
from app.database.models.subscription import Subscription
from app.database.models.payment import Payment, PaymentReceipt, BankCard, get_active_bank_card_or_fallback
from app.modules.orders.service import OrderService
from app.modules.subscriptions.service import SubscriptionService
from app.bot.locales.fa import MAIN_MENU_BTNS, format_toman
from app.modules.bot_texts.service import get_bot_text
from app.bot.keyboards.inline import (
    get_products_keyboard,
    get_locations_keyboard,
    get_payment_methods_keyboard,
    get_admin_payment_approval_keyboard,
)
from app.bot.states.customer import PurchaseOrderState
from app.core.constants import PaymentMethod, PaymentStatus, OrderStatus
from app.core.config import settings
from app.bot.notifications import notify_all_admins

logger = logging.getLogger(__name__)

router = Router()


def generate_qr_buffer(content: str) -> io.BytesIO:
    """Generate QR code in memory as PNG."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@router.message(F.text == MAIN_MENU_BTNS["buy"])
async def start_purchase(message: Message, state: FSMContext, db_session: AsyncSession):
    """List available plans for purchase."""
    await state.clear()
    products_res = await db_session.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.sort_order.asc())
    )
    products = list(products_res.scalars().all())

    if not products:
        await message.answer("⚠️ در حال حاضر هیچ پلن فعالی جهت فروش وجود ندارد. لطفاً بعداً بررسی نمایید.")
        return

    await state.set_state(PurchaseOrderState.selecting_product)
    purchase_guide_text = get_bot_text(
        "purchase_guide",
        "🛒 <b>لطفاً یکی از بسته‌های اینترنت آزاد زیر را انتخاب فرمایید:</b>",
    )
    await message.answer(
        purchase_guide_text,
        reply_markup=get_products_keyboard(products),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("buy_prod:"), PurchaseOrderState.selecting_product)
async def handle_product_selected(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    product_id = callback.data.split(":")[1]
    await state.update_data(product_id=product_id)

    product = None
    try:
        product = await db_session.get(Product, uuid.UUID(product_id))
    except Exception:
        pass

    if not product:
        await callback.answer("پلن مورد نظر یافت نشد.", show_alert=True)
        return

    loc_mode = getattr(product, "location_mode", None)

    import html
    p_name = html.escape(product.name)

    # Case 1: Unlimited plan (traffic_bytes == 0) -> Multi-location across all allowed
    if product.traffic_bytes == 0:
        allowed_locs = product.allowed_locations or "ALL"
        await state.update_data(location_code=allowed_locs)
        await state.set_state(PurchaseOrderState.entering_coupon)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏩ رد شدن (بدون کد تخفیف)", callback_data="skip_coupon")],
                [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")],
            ]
        )
        await callback.message.edit_text(
            f"✨ <b>بسته نامحدود «{p_name}» انتخاب شد.</b>\n\n"
            f"🌐 <b>این اشتراک شامل دسترسی همزمان به تمامی لوکیشن‌های مجاز این پلن می‌باشد</b> و نیازی به انتخاب تک‌لوکیشن ندارد.\n\n"
            f"🎁 <b>آیا کد تخفیف دارید؟</b>\nاگر کد تخفیف دارید آن را تایپ و ارسال نمایید، در غیر این صورت دکمه «رد شدن» را لمس کنید:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Case 2: Explicit multi-location plan configured by admin (all or custom with 2+ locations)
    if loc_mode in ("all", "custom"):
        allowed_list = [loc.strip() for loc in (product.allowed_locations or "").split(",") if loc.strip() and loc.strip().upper() != "ALL"]
        if len(allowed_list) != 1:
            allowed_locs = product.allowed_locations or "ALL"
            await state.update_data(location_code=allowed_locs)
            await state.set_state(PurchaseOrderState.entering_coupon)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⏩ رد شدن (بدون کد تخفیف)", callback_data="skip_coupon")],
                    [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")],
                ]
            )
            loc_desc = "تمامی لوکیشن‌های فعال سرورها" if (not product.allowed_locations or product.allowed_locations.strip().upper() == "ALL") else "تمامی لوکیشن‌های اختصاصی این پلن"
            await callback.message.edit_text(
                f"✨ <b>بسته «{p_name}» انتخاب شد.</b>\n\n"
                f"🌐 <b>این اشتراک شامل دسترسی همزمان به {loc_desc} می‌باشد</b> و نیازی به انتخاب تک‌لوکیشن ندارد.\n\n"
                f"🎁 <b>آیا کد تخفیف دارید؟</b>\nاگر کد تخفیف دارید آن را تایپ و ارسال نمایید، در غیر این صورت دکمه «رد شدن» را لمس کنید:",
                reply_markup=kb,
                parse_mode="HTML",
            )
            await callback.answer()
            return

    # Case 3: Single location plan or Customer chooses 1 location (user_choice or legacy volume plan)
    servers_res = await db_session.execute(
        select(Server).where(Server.is_active.is_(True))
    )
    servers = list(servers_res.scalars().all())

    # Filter servers if product restricts allowed_locations
    if product.allowed_locations and product.allowed_locations.strip().upper() != "ALL":
        allowed_list = [loc.strip().upper() for loc in product.allowed_locations.split(",") if loc.strip()]
        filtered_servers = [
            s for s in servers
            if any(
                s.location_code.upper() == a
                or s.location_code.upper().startswith(a + "-")
                or (s.location_code.upper().split("-")[0] == a)
                or a in s.location_code.upper()
                for a in allowed_list
            )
        ]
        if filtered_servers:
            servers = filtered_servers

    # Auto-assign location if only 1 server available in the allowed list
    auto_loc = None
    if product.allowed_locations and product.allowed_locations.strip().upper() != "ALL":
        allowed_list = [loc.strip() for loc in product.allowed_locations.split(",") if loc.strip()]
        if len(allowed_list) == 1:
            auto_loc = allowed_list[0]

    if not auto_loc and len(servers) == 1:
        auto_loc = servers[0].location_code or "DEFAULT"

    if auto_loc:
        await state.update_data(location_code=auto_loc)
        await state.set_state(PurchaseOrderState.entering_coupon)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⏩ رد شدن (بدون کد تخفیف)", callback_data="skip_coupon")],
                [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")],
            ]
        )
        loc_label = auto_loc
        if servers:
            for s in servers:
                if s.location_code and s.location_code.upper() == auto_loc.upper():
                    flag_icon = getattr(s, "flag_emoji", getattr(s, "flag", "🌍"))
                    loc_title = getattr(s, "location_name", getattr(s, "country_name", getattr(s, "name", auto_loc)))
                    loc_label = f"{flag_icon} {loc_title}"
                    break
            else:
                if len(servers) == 1:
                    s = servers[0]
                    flag_icon = getattr(s, "flag_emoji", getattr(s, "flag", "🌍"))
                    loc_title = getattr(s, "location_name", getattr(s, "country_name", getattr(s, "name", auto_loc)))
                    loc_label = f"{flag_icon} {loc_title}"

        await callback.message.edit_text(
            f"✨ <b>بسته «{p_name}» انتخاب شد.</b>\n"
            f"📍 <b>موقعیت مکانی:</b> <b>{loc_label}</b> (انتخاب خودکار)\n\n"
            f"🎁 <b>آیا کد تخفیف دارید؟</b>\nاگر کد تخفیف دارید آن را تایپ و ارسال نمایید، در غیر این صورت دکمه «رد شدن» را لمس کنید:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.set_state(PurchaseOrderState.selecting_location)
    await callback.message.edit_text(
        "🌍 <b>لطفاً موقعیت مکانی (لوکیشن) سرور مورد نظر خود را انتخاب نمایید:</b>",
        reply_markup=get_locations_keyboard(servers),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sel_loc:"), PurchaseOrderState.selecting_location)
async def handle_location_selected(callback: CallbackQuery, state: FSMContext):
    location_code = callback.data.split(":")[1]
    await state.update_data(location_code=location_code)

    await state.set_state(PurchaseOrderState.entering_coupon)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ رد شدن (بدون کد تخفیف)", callback_data="skip_coupon")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
    ])
    await callback.message.edit_text(
        "🎁 <b>آیا کد تخفیف دارید؟</b>\n\nاگر کد تخفیف دارید آن را تایپ و ارسال کنید، در غیر این صورت دکمه «رد شدن» را لمس نمایید:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "skip_coupon", PurchaseOrderState.entering_coupon)
async def handle_skip_coupon(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    await proceed_to_order_confirmation(callback.message, state, db_user, db_session, coupon_code=None)
    await callback.answer()


@router.message(PurchaseOrderState.entering_coupon)
async def handle_coupon_input(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    coupon_code = message.text.strip()
    await proceed_to_order_confirmation(message, state, db_user, db_session, coupon_code=coupon_code)


async def proceed_to_order_confirmation(
    target_msg: Message,
    state: FSMContext,
    db_user: User,
    db_session: AsyncSession,
    coupon_code: str = None,
):
    data = await state.get_data()
    product_id = uuid.UUID(data["product_id"])
    product = await db_session.get(Product, product_id)
    is_unlimited = bool(product and product.traffic_bytes == 0)

    location_code = data.get("location_code", "ALL")

    # Match selected location to a specific active server (only for volume plans)
    selected_server = None
    if not is_unlimited and location_code and location_code.upper() != "ALL":
        target = location_code.upper()
        base_target = target.split("-")[0]
        srv_res = await db_session.execute(
            select(Server).where(
                and_(
                    Server.is_active.is_(True),
                    or_(
                        Server.location_code == target,
                        Server.location_code.like(f"{target}-%"),
                        Server.location_code.like(f"{base_target}-%"),
                        Server.location_code == base_target,
                    ),
                )
            ).order_by(Server.priority.desc())
        )
        selected_server = srv_res.scalars().first()

    order_service = OrderService(db_session)
    order = await order_service.create_order(
        user_id=db_user.id,
        product_id=product_id,
        server_id=selected_server.id if selected_server else None,
        coupon_code=coupon_code,
    )

    # Check user wallet balance
    wallet = await db_session.execute(select(Wallet).where(Wallet.user_id == db_user.id))
    user_wallet = wallet.scalar_one_or_none()
    can_wallet = bool(user_wallet and user_wallet.balance_irr >= order.total_price_irr)

    await state.update_data(order_id=str(order.id), product_id=str(product.id))
    await state.set_state(PurchaseOrderState.selecting_payment_method)

    # Immediately alert admins of new incoming order
    import html
    u_username = f"@{html.escape(db_user.username)}" if db_user.username else "ندارد"
    u_name = html.escape(db_user.first_name or "")
    loc_admin_display = "🌐 تمام لوکیشن‌های پلن (نامحدود)" if is_unlimited else html.escape(str(location_code))
    traffic_admin_display = "♾ نامحدود" if is_unlimited else f"{product.traffic_gb} گیگابایت"

    coupon_admin_line = ""
    coupon_user_line = ""
    if coupon_code:
        if order.coupon_id:
            coupon_admin_line = f"🎟 <b>کد تخفیف اعمال‌شده:</b> <code>{html.escape(coupon_code)}</code>\n"
            orig_toman = (product.price_irr // 10) if product and product.price_irr else order.total_price_toman
            diff_toman = max(0, orig_toman - order.total_price_toman)
            coupon_user_line = (
                f"🎉 <b>کد تخفیف اعمال شد:</b> <code>{html.escape(coupon_code)}</code> (تخفیف: {diff_toman:,} تومان)\n"
            )
        else:
            coupon_admin_line = f"⚠️ <b>کد تخفیف ناموفق:</b> <code>{html.escape(coupon_code)}</code>\n"
            coupon_user_line = f"⚠️ <i>توجه: کد تخفیف «{html.escape(coupon_code)}» نامعتبر یا منقضی شده است.</i>\n"

    admin_order_alert = (
        f"🛒 <b>سفارش جدید در ربات ثبت شد!</b>\n\n"
        f"👤 <b>مشتری:</b> {u_name} ({u_username})\n"
        f"🆔 <b>شناسه تلگرام:</b> <code>{db_user.telegram_id}</code>\n"
        f"📦 <b>پلن:</b> {html.escape(product.name)}\n"
        f"📊 <b>ترافیک:</b> <b>{traffic_admin_display}</b>\n"
        f"🌍 <b>لوکیشن:</b> <code>{loc_admin_display}</code>\n"
        f"{coupon_admin_line}"
        f"💰 <b>مبلغ قابل پرداخت:</b> <b>{order.total_price_toman:,} تومان</b>\n"
        f"🔖 <b>شناسه سفارش:</b> <code>{order.id}</code>\n\n"
        f"⏳ کاربر به مرحله انتخاب روش پرداخت و ارسال فیش منتقل شد."
    )
    for admin_tg_id in settings.ADMIN_TELEGRAM_IDS:
        try:
            bot = target_msg.bot
            await bot.send_message(chat_id=admin_tg_id, text=admin_order_alert, parse_mode="HTML")
        except Exception as e_adm:
            logger.warning(f"Failed to alert admin {admin_tg_id} of new order: {e_adm}")

    c2c_info = ""
    if getattr(settings, "CARD_TO_CARD_ENABLED", False):
        card_info = await get_active_bank_card_or_fallback(db_session)
        c2c_info = (
            f"\n💳 <b>اطلاعات پرداخت کارت به کارت:</b>\n"
            f"🏦 بانک: <b>{html.escape(card_info['bank_name'])}</b>\n"
            f"🔢 شماره کارت:\n<code>{card_info['card_number']}</code>\n"
            f"👤 به نام: <b>{html.escape(card_info['card_holder'])}</b>\n\n"
            f"📸 <i>پس از واریز، کافیست تصویر فیش را مستقیماً در همین بخش ارسال نمایید.</i>\n"
        )

    traffic_display = "<b>♾ نامحدود</b>" if is_unlimited else f"<b>{product.traffic_gb} گیگابایت</b>"
    loc_display = (
        "<b>🌐 تمام لوکیشن‌های مجاز پلن (دسترسی همزمان)</b>"
        if is_unlimited
        else f"<b>{html.escape(str(location_code))}</b>"
    )

    text = (
        f"🧾 <b>پیش‌فاکتور سفارش شما:</b>\n\n"
        f"📦 سرویس: <b>{html.escape(product.name)}</b>\n"
        f"⏳ مدت اعتبار: <b>{product.duration_days} روز</b>\n"
        f"📊 حجم ترافیک: {traffic_display}\n"
        f"🌍 لوکیشن: {loc_display}\n"
        f"{coupon_user_line}"
        f"💰 مبلغ نهایی قابل پرداخت: <b>{order.total_price_toman:,} تومان</b>\n"
        f"💳 موجودی کیف پول شما: <b>{user_wallet.balance_toman if user_wallet else 0:,} تومان</b>\n"
        f"{c2c_info}\n"
        f"👇 <b>یا روش پرداخت مورد نظر خود را انتخاب نمایید:</b>"
    )

    active_provs = {
        "card_to_card": getattr(settings, "CARD_TO_CARD_ENABLED", False),
        "crypto": getattr(settings, "CRYPTO_ENABLED", False),
        "perfect_money": getattr(settings, "PERFECT_MONEY_ENABLED", False),
        "zarinpal": getattr(settings, "ZARINPAL_ENABLED", False),
        "sep": getattr(settings, "SEP_ENABLED", False),
        "idpay": getattr(settings, "IDPAY_ENABLED", False),
        "stripe": getattr(settings, "STRIPE_ENABLED", False),
    }
    try:
        from app.database.models.payment import PaymentGateway
        gw_res = await db_session.execute(select(PaymentGateway.provider).where(PaymentGateway.is_active == True))
        for p_name in gw_res.scalars().all():
            active_provs[p_name.lower()] = True
    except Exception:
        pass

    kb = get_payment_methods_keyboard(str(order.id), can_wallet=can_wallet, active_providers=active_provs)
    if hasattr(target_msg, "edit_text"):
        try:
            await target_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await target_msg.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("pay_method:"))
async def handle_payment_selection(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    _, order_id_str, method = callback.data.split(":")
    if method != PaymentMethod.CARD_TO_CARD:
        try:
            await callback.answer()
        except Exception:
            pass
    order_id = uuid.UUID(order_id_str)
    order_service = OrderService(db_session)
    sub_service = SubscriptionService(db_session)

    payment, init_res = await order_service.initiate_payment(order_id, method)

    if method == PaymentMethod.WALLET:
        # Immediate provisioning succeeded
        sub_res = await db_session.execute(
            select(Subscription).where(Subscription.order_id == order_id)
        )
        subscription = sub_res.scalar_one_or_none()
        conn = await sub_service.get_connection_details(subscription.id)

        qr_buf = generate_qr_buffer(conn["subscription_url"])
        qr_file = BufferedInputFile(qr_buf.getvalue(), filename="vpn_sub_qr.png")

        success_msg = (
            f"🎉 <b>خرید شما با موفقیت انجام شد و سرویس فعال گردید!</b>\n\n"
            f"🏷 شناسه سرویس: <code>{subscription.client_email}</code>\n"
            f"🌍 لوکیشن: {conn['flag']} <b>{conn['location']}</b>\n"
            f"📡 پروتکل: <b>{conn['protocol']}</b>\n\n"
            f"🔗 <b>لینک اشتراک هوشمند (Subscription URL):</b>\n"
            f"<code>{conn['subscription_url']}</code>\n\n"
            f"📱 <b>نرم‌افزار پیشنهادی جهت اتصال (Happ Proxy):</b>\n"
            f"🍏 <b>دانلود نسخه iOS (آیفون):</b> <a href=\"https://apps.apple.com/app/happ-proxy-utility/id6504287215\">دریافت از App Store</a>\n"
            f"🤖 <b>دانلود نسخه اندروید:</b> <a href=\"https://play.google.com/store/apps/details?id=com.happproxy\">دریافت از Google Play</a>\n\n"
            f"💡 <i>کافیست پس از نصب برنامه، لینک اشتراک هوشمند بالا را کپی کرده یا بارکد QR بالا را درون نرم‌افزار اسکن نمایید.</i>"
        )
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=qr_file,
            caption=success_msg,
            parse_mode="HTML",
        )
        await state.clear()

        # Broadcast sale to admins
        try:
            import html
            u_name = html.escape(db_user.first_name or "کاربر")
            u_user = f" (@{html.escape(db_user.username)})" if db_user.username else ""
            order = await db_session.get(Order, order_id)
            prod_name = ""
            if order and order.product_id:
                prod = await db_session.get(Product, order.product_id)
                if prod:
                    prod_name = f"\n📦 <b>بسته:</b> <b>{html.escape(prod.name)}</b>"

            admin_sale_msg = (
                f"🛒 <b>#فروش_موفق_جدید (کیف پول)</b>\n\n"
                f"👤 <b>مشتری:</b> {u_name}{u_user}\n"
                f"🆔 <b>شناسه مشتری:</b> <code>{db_user.telegram_id}</code>{prod_name}\n"
                f"💰 <b>مبلغ کسر شده:</b> <b>{payment.amount_toman:,} تومان</b>\n"
                f"🏷 <b>شناسه اکانت:</b> <code>{subscription.client_email}</code>\n"
                f"🌍 <b>لوکیشن:</b> {conn['flag']} <b>{conn['location']}</b>\n"
                f"📡 <b>پروتکل:</b> <b>{conn['protocol']}</b>\n"
                f"✅ <b>وضعیت:</b> کسر خودکار از کیف پول و فعال‌سازی فوری در پنل"
            )
            await notify_all_admins(bot=callback.bot, db_session=db_session, text=admin_sale_msg)
        except Exception as e_sale:
            logger.warning(f"Failed to notify admins of wallet sale: {e_sale}")

    elif method == PaymentMethod.CARD_TO_CARD:
        card_info = await get_active_bank_card_or_fallback(db_session)
        card_number = card_info["card_number"]
        card_bank_name = card_info["bank_name"]
        card_holder = card_info["card_holder"]

        await state.update_data(payment_id=str(payment.id), order_id=str(order_id), card_number=card_number)
        await state.set_state(PurchaseOrderState.awaiting_card_receipt)
        await db_session.commit()
        import html
        card_guide = get_bot_text(
            "card_payment_guide",
            "📸 <b>مرحله نهایی: لطفاً تصویر فیش یا رسید واریزی خود را در همین بخش ارسال نمایید:</b>\n<i>(کافیست عکس رسید را مستقیماً مانند یک عکس یا پیام عادی به همین چت بفرستید)</i>",
        )
        c2c_text = (
            f"💳 <b>اطلاعات واریز کارت به کارت:</b>\n\n"
            f"🏦 بانک: <b>{html.escape(card_bank_name)}</b>\n"
            f"🔢 شماره کارت (جهت کپی لمس نمایید):\n<code>{card_number}</code>\n"
            f"👤 به نام: <b>{html.escape(card_holder)}</b>\n"
            f"💰 مبلغ دقیق: <b>{payment.amount_toman:,} تومان</b>\n"
            f"🔖 شناسه پیگیری سیستم: <code>{payment.tracking_code}</code>\n\n"
            f"{card_guide}"
        )
        c2c_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 کپی شماره کارت", callback_data=f"copy_card:{card_number}")],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
        ])
        try:
            await callback.message.edit_text(c2c_text, reply_markup=c2c_kb, parse_mode="HTML")
        except Exception:
            await callback.message.answer(c2c_text, reply_markup=c2c_kb, parse_mode="HTML")

        try:
            await callback.answer(
                f"💳 شماره کارت: {card_number}\nبانک {card_bank_name} ({card_holder})\n\nلطفاً پس از واریز مبلغ، تصویر فیش را ارسال فرمایید.",
                show_alert=True
            )
        except Exception:
            pass
        return

    elif method == PaymentMethod.CRYPTO:
        trc20_enabled = getattr(settings, "CRYPTO_TRC20_ENABLED", True)
        bep20_enabled = getattr(settings, "CRYPTO_BEP20_ENABLED", False)

        if trc20_enabled and bep20_enabled:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="⚡️ TRC-20 (Tron)", callback_data=f"crypto_net:{payment.id}:{order_id}:trc20"),
                    InlineKeyboardButton(text="🟡 BEP-20 (BSC)", callback_data=f"crypto_net:{payment.id}:{order_id}:bep20"),
                ],
                [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
            ])
            prompt_text = (
                f"🪙 <b>انتخاب شبکه انتقال تتر (USDT):</b>\n\n"
                f"💰 مبلغ قابل پرداخت: <b>{payment.amount_toman:,} تومان</b>\n"
                f"🔖 شناسه سفارش: <code>{payment.tracking_code}</code>\n\n"
                f"لطفاً شبکه انتقال تتر مورد نظر خود را انتخاب نمایید:\n"
                f"• <b>TRC-20 (Tron):</b> شبکه ترون\n"
                f"• <b>BEP-20 (BSC):</b> شبکه هوشمند بایننس (Binance Smart Chain)"
            )
            try:
                await callback.message.edit_text(prompt_text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await callback.message.answer(prompt_text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()
            return
        else:
            network = "bep20" if (bep20_enabled and not trc20_enabled) else "trc20"
            await send_crypto_instructions(
                target_msg=callback.message,
                state=state,
                payment=payment,
                order_id=order_id,
                network=network,
            )
            await callback.answer()
            return

    elif method == PaymentMethod.ZARINPAL:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ورود به صفحه پرداخت زرین‌پال", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
        ])
        await callback.message.edit_text(
            f"💳 <b>شناسه پرداخت شما:</b> <code>{payment.tracking_code}</code>\n\n"
            f"جهت تکمیل پرداخت مبلغ <b>{payment.amount_toman:,} تومان</b> بر روی دکمه زیر کلیک کنید. پس از پرداخت آنلاین، سرویس شما بلافاصله فعال خواهد شد.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()

    elif method == PaymentMethod.SEP:
        if init_res.status == "FAILED":
            await callback.message.answer(
                f"⚠️ {init_res.instructions or 'خطا در اتصال به درگاه سامان.'}",
                parse_mode="HTML",
            )
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ورود به درگاه سامان شاپرک (SEP)", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
        ])
        await callback.message.edit_text(
            f"💳 <b>درگاه پرداخت الکترونیک سامان (شاپرک)</b>\n\n"
            f"🔖 <b>شناسه سفارش شما:</b> <code>{payment.tracking_code}</code>\n"
            f"💰 <b>مبلغ قابل پرداخت:</b> <b>{payment.amount_toman:,} تومان</b>\n\n"
            f"جهت پرداخت امن شاپرک بر روی دکمه زیر کلیک نمایید. به محض تکمیل پرداخت، سرویس شما به صورت آنی فعال و تحویل داده می‌شود.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()

    elif method == PaymentMethod.IDPAY:
        if init_res.status == "FAILED":
            await callback.message.answer(
                f"⚠️ {init_res.instructions or 'خطا در اتصال به درگاه آیدی پی.'}",
                parse_mode="HTML",
            )
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ورود به درگاه پرداخت آیدی پی (IDPay)", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
        ])
        await callback.message.edit_text(
            f"💳 <b>درگاه پرداخت آنلاین آیدی پی (IDPay)</b>\n\n"
            f"🔖 <b>شناسه سفارش شما:</b> <code>{payment.tracking_code}</code>\n"
            f"💰 <b>مبلغ قابل پرداخت:</b> <b>{payment.amount_toman:,} تومان</b>\n\n"
            f"جهت پرداخت امن، بر روی دکمه زیر کلیک نمایید. به محض تکمیل پرداخت، سرویس شما به صورت آنی فعال و تحویل داده می‌شود.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()

    elif method == PaymentMethod.PERFECT_MONEY:
        pm_account = getattr(settings, "PERFECT_MONEY_ACCOUNT", "U12345678")
        rate = getattr(settings, "PERFECT_MONEY_USD_RATE", 70000)
        usd_amount = round(payment.amount_toman / rate, 2)
        if usd_amount < 1.0:
            usd_amount = 1.0

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 ورود به درگاه پرفکت مانی (Perfect Money)", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
        ])
        await callback.message.edit_text(
            f"💵 <b>پرداخت با پرفکت مانی (Perfect Money)</b>\n\n"
            f"💰 مبلغ دلاری: <b>{usd_amount} USD</b>\n"
            f"💵 معادل تومانی: <b>{payment.amount_toman:,} تومان</b> (نرخ هر دلار: {rate:,} تومان)\n"
            f"📍 حساب پرفکت مانی: <code>{pm_account}</code>\n"
            f"🔖 شناسه سفارش: <code>{payment.tracking_code}</code>\n\n"
            f"جهت تکمیل پرداخت بر روی دکمه زیر کلیک نمایید. پس از واریز، سرویس شما به صورت خودکار و آنی تحویل داده می‌شود.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()

    elif method == PaymentMethod.STRIPE:
        if init_res.status == "FAILED" or not init_res.redirect_url:
            await callback.message.answer(
                f"⚠️ {init_res.instructions or 'خطا در اتصال به درگاه بین‌المللی استرایپ.'}",
                parse_mode="HTML",
            )
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 ورود به درگاه بین‌المللی استرایپ (Stripe)", url=init_res.redirect_url)],
            [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
        ])
        await callback.message.edit_text(
            f"💳 <b>درگاه پرداخت بین‌المللی استرایپ (Stripe - Visa / Mastercard / Apple Pay)</b>\n\n"
            f"🔖 <b>شناسه سفارش شما:</b> <code>{payment.tracking_code}</code>\n"
            f"💰 <b>مبلغ معادل تومانی:</b> <b>{payment.amount_toman:,} تومان</b>\n\n"
            f"جهت پرداخت ارزی با کارت‌های بانکی بین‌المللی بر روی دکمه زیر کلیک نمایید. به محض تکمیل پرداخت، سرویس شما به صورت خودکار و آنی فعال و تحویل داده می‌شود.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await state.clear()

    await callback.answer()


@router.callback_query(F.data.startswith("copy_card:"))
async def handle_copy_card(callback: CallbackQuery):
    card = callback.data.split(":", 1)[1]
    await callback.answer(f"📋 شماره کارت:\n{card}\n\nجهت واریز مبلغ در اپلیکیشن بانکی آماده است.", show_alert=True)


async def send_crypto_instructions(
    target_msg: Message,
    state: FSMContext,
    payment: Payment,
    order_id: uuid.UUID,
    network: str,
):
    await state.update_data(payment_id=str(payment.id), order_id=str(order_id), crypto_network=network)
    await state.set_state(PurchaseOrderState.awaiting_card_receipt)

    rate = getattr(settings, "USDT_TOMAN_RATE", 70000)
    usdt_amount = round(payment.amount_toman / rate, 2)
    if usdt_amount < 1.0:
        usdt_amount = 1.0

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

    caption = (
        f"🪙 <b>اطلاعات پرداخت با ارز دیجیتال (تتر USDT - شبکه {net_title}):</b>\n\n"
        f"💰 مبلغ دلاری: <b>{usdt_amount} USDT</b>\n"
        f"💵 معادل تومانی: <b>{payment.amount_toman:,} تومان</b> (نرخ هر تتر: {rate:,} تومان)\n"
        f"🔖 شناسه پیگیری سیستم: <code>{payment.tracking_code}</code>\n\n"
        f"📍 <b>آدرس کیف پول تتر ({net_title}):</b>\n<code>{wallet_addr}</code>\n\n"
        f"{net_alert}\n\n"
        f"📸 پس از پرداخت، لطفاً <b>تصویر رسید یا کد هش (TXID)</b> تراکنش را در همین بخش ارسال نمایید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 کپی آدرس کیف پول", callback_data=f"copy_crypto:{wallet_addr}")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel_action")]
    ])

    try:
        await target_msg.delete()
    except Exception:
        pass
    await target_msg.answer_photo(
        photo=qr_file,
        caption=caption,
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("crypto_net:"))
async def handle_crypto_network_selection(
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
):
    parts = callback.data.split(":")
    payment_id = uuid.UUID(parts[1])
    order_id = uuid.UUID(parts[2])
    network = parts[3]

    payment = await db_session.get(Payment, payment_id)
    if not payment:
        await callback.answer("پرداخت یافت نشد.", show_alert=True)
        return

    await send_crypto_instructions(
        target_msg=callback.message,
        state=state,
        payment=payment,
        order_id=order_id,
        network=network,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("copy_crypto:"))
async def handle_copy_crypto(callback: CallbackQuery):
    addr = callback.data.split(":", 1)[1]
    await callback.answer(f"📍 آدرس کیف پول:\n{addr}\n\nجهت انتقال کپی شد.", show_alert=True)



@router.message(
    PurchaseOrderState.awaiting_card_receipt,
    F.photo | F.document | (F.text & ~F.text.startswith("/"))
)
@router.message(
    PurchaseOrderState.selecting_payment_method,
    F.photo | F.document
)
async def handle_card_receipt_photo(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    """Customer uploads photo, document receipt, or TXID text for Card-to-Card or Crypto with resilient fallback."""
    current_state = await state.get_state()
    # Do not intercept if user is in an admin, support, wallet, or renewal state
    if current_state and any(str(current_state).startswith(s) for s in ("AdminState", "SupportTicketState", "WalletTopupState", "SubRenewState")):
        return

    data = await state.get_data()
    order_service = OrderService(db_session)
    payment = None

    # 1. Resolve payment by payment_id in FSM data
    payment_id_str = data.get("payment_id")
    if payment_id_str:
        try:
            payment = await db_session.get(Payment, uuid.UUID(payment_id_str))
        except Exception:
            payment = None

    # 2. Resolve payment by order_id in FSM data
    if not payment:
        order_id_str = data.get("order_id")
        if order_id_str:
            try:
                order = await db_session.get(Order, uuid.UUID(order_id_str))
                if order and order.user_id == db_user.id:
                    pay_res = await db_session.execute(
                        select(Payment)
                        .where(Payment.order_id == order.id, Payment.status == PaymentStatus.PENDING)
                        .order_by(Payment.created_at.desc())
                    )
                    payment = pay_res.scalars().first()
                    if not payment:
                        payment, _ = await order_service.initiate_payment(order.id, PaymentMethod.CARD_TO_CARD)
            except Exception as e:
                logger.warning(f"Failed to resolve order from FSM data: {e}")

    # 3. Resolve payment by latest open order in DB
    if not payment:
        order_res = await db_session.execute(
            select(Order)
            .where(Order.user_id == db_user.id)
            .where(Order.status.in_([OrderStatus.CREATED, OrderStatus.PAYMENT_PENDING]))
            .order_by(Order.created_at.desc())
        )
        latest_order = order_res.scalars().first()
        if latest_order:
            pay_res = await db_session.execute(
                select(Payment)
                .where(Payment.order_id == latest_order.id, Payment.status == PaymentStatus.PENDING)
                .order_by(Payment.created_at.desc())
            )
            payment = pay_res.scalars().first()
            if not payment:
                payment, _ = await order_service.initiate_payment(latest_order.id, PaymentMethod.CARD_TO_CARD)

    # 4. If absolutely no order found for user:
    if not payment:
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 خرید اشتراک جدید", callback_data="open_buy_menu")]
        ])
        return await message.answer(
            "⚠️ <b>هیچ سفارشی در انتظار پرداخت برای شما یافت نشد!</b>\n\n"
            "لطفاً ابتدا از بخش <b>🛒 خرید اشتراک</b> پلن مد نظر خود را انتخاب کرده و سپس تصویر فیش واریزی را ارسال فرمایید.",
            reply_markup=kb,
            parse_mode="HTML"
        )

    # Validate file type and size
    is_text_txid = False
    txid_val = None
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        doc = message.document
        if doc.file_size and doc.file_size > 15 * 1024 * 1024:  # 15MB limit
            return await message.answer("⚠️ حجم فایل ارسالی بیش از حد مجاز (۱۵ مگابایت) است.")
        file_id = doc.file_id
    elif message.text:
        is_text_txid = True
        txid_val = message.text.strip()
    else:
        return await message.answer("⚠️ لطفاً تصویر فیش یا کد هش تراکنش خود را ارسال فرمایید.")

    # Check if receipt record already exists to prevent duplicate key constraint crashes
    receipt_res = await db_session.execute(
        select(PaymentReceipt).where(PaymentReceipt.payment_id == payment.id)
    )
    receipt = receipt_res.scalar_one_or_none()

    if receipt:
        receipt.telegram_file_id = file_id
        receipt.submitted_amount_toman = payment.amount_toman
        if txid_val:
            receipt.transaction_ref = txid_val
    else:
        receipt = PaymentReceipt(
            id=uuid.uuid4(),
            payment_id=payment.id,
            telegram_file_id=file_id,
            transaction_ref=txid_val,
            submitted_amount_toman=payment.amount_toman,
        )
        db_session.add(receipt)
    if txid_val:
        payment.reference_id = txid_val
    await db_session.commit()

    order = await db_session.get(Order, payment.order_id) if payment.order_id else None
    user_coupon_note = ""
    coupon_info = ""
    product_name = ""
    if order and order.product_id:
        prod = await db_session.get(Product, order.product_id)
        if prod:
            product_name = f"\n📦 <b>پلن انتخابی:</b> {html.escape(prod.name)}"
        if order.coupon_id:
            from app.database.models.reseller import Coupon
            cpn = await db_session.get(Coupon, order.coupon_id)
            if cpn:
                orig_toman = (prod.price_irr // 10) if (prod and prod.price_irr) else payment.amount_toman
                diff_toman = max(0, orig_toman - payment.amount_toman)
                user_coupon_note = f"\n🎟 <b>کد تخفیف:</b> <code>{html.escape(cpn.code)}</code> ({cpn.discount_percent}%)"
                coupon_info = (
                    f"\n🎟 <b>کد تخفیف:</b> <code>{html.escape(cpn.code)}</code>"
                    f" ({cpn.discount_percent}% تخفیف - کسر: {diff_toman:,} تومان)"
                    f"\n💵 <b>مبلغ اصلی:</b> <s>{orig_toman:,}</s> تومان"
                )

    method_fa = "ارز دیجیتال (تتر TRC-20)" if payment.provider == PaymentMethod.CRYPTO else "کارت به کارت"
    await message.answer(
        f"✅ <b>رسید پرداخت ({method_fa}) شما با موفقیت ثبت شد.</b>\n\n"
        f"💰 <b>مبلغ فیش:</b> <b>{payment.amount_toman:,} تومان</b>{user_coupon_note}\n"
        f"🔖 <b>شناسه پیگیری:</b> <code>{payment.tracking_code}</code>\n\n"
        "⏳ رسید برای مدیریت ارسال شد و به محض تایید، سرویس شما فعال و تحویل داده می‌شود.",
        parse_mode="HTML",
    )
    await state.clear()

    # Notify Admins with receipt photo and approval buttons using safe HTML formatting
    import html
    u_username = f"@{html.escape(db_user.username)}" if db_user.username else "ندارد"
    u_name = html.escape(db_user.first_name or "")

    admin_caption = (
        f"🔔 <b>رسید واریزی جدید جهت تایید ({html.escape(method_fa)})</b>\n\n"
        f"👤 <b>کاربر:</b> {u_name} ({u_username})\n"
        f"🆔 <b>شناسه کاربر:</b> <code>{db_user.telegram_id}</code>{product_name}{coupon_info}\n"
        f"💰 <b>مبلغ فیش:</b> <b>{payment.amount_toman:,} تومان</b>\n"
        f"🔖 <b>کد پیگیری:</b> <code>{payment.tracking_code}</code>\n"
    )
    if is_text_txid and txid_val:
        admin_caption += f"🔗 <b>کد رهگیری / TXID:</b>\n<code>{html.escape(txid_val)}</code>\n"

    approval_kb = get_admin_payment_approval_keyboard(str(payment.id))

    await notify_all_admins(
        bot=bot,
        db_session=db_session,
        text=admin_caption,
        photo_file_id=file_id if message.photo else None,
        document_file_id=file_id if message.document else None,
        reply_markup=approval_kb,
    )
    logger.info(f"Broadcasted pending receipt {payment.tracking_code} to all admins.")


@router.callback_query(F.data == "open_buy_menu")
async def handle_open_buy_menu(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await callback.message.delete()
    await start_purchase(callback.message, state, db_session)
    await callback.answer()


@router.callback_query(F.data == "back_to_products")
async def handle_back_to_products(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await state.clear()
    products_res = await db_session.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.sort_order.asc())
    )
    products = list(products_res.scalars().all())
    await state.set_state(PurchaseOrderState.selecting_product)
    await callback.message.edit_text(
        "🛒 <b>لطفاً یکی از بسته‌های اینترنت آزاد زیر را انتخاب فرمایید:</b>",
        reply_markup=get_products_keyboard(products),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_action")
async def handle_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("عملیات با موفقیت لغو شد.")
    await callback.answer()
