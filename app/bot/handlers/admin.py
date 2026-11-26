"""Admin Telegram management panel, payment approval, and server telemetry."""
import io
import html
import logging
import uuid
from datetime import datetime, timezone, timedelta
import qrcode
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, BufferedInputFile, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, and_, or_, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User
from app.database.models.payment import Payment, PaymentReceipt
from app.database.models.panel import Panel, Server, Inbound
from app.database.models.subscription import Subscription
from app.database.models.product import Product
from app.database.models.reseller import Coupon
from app.database.models.support import SupportTicket, SupportMessage
from app.modules.analytics.service import AnalyticsService
from app.modules.orders.service import OrderService
from app.modules.subscriptions.service import SubscriptionService
from app.modules.wallets.service import WalletService
from app.bot.locales.fa import MAIN_MENU_BTNS
from app.bot.states.customer import AdminState
from app.core.constants import UserRole, PaymentStatus, SubscriptionStatus, TicketStatus, PaymentMethod
from app.core.config import settings
from app.bot.notifications import notify_all_admins

logger = logging.getLogger(__name__)
router = Router()


def is_admin(user: User) -> bool:
    if not user:
        return False
    return (
        user.telegram_id in settings.ADMIN_TELEGRAM_IDS
        or user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.FINANCE, UserRole.SUPPORT)
    )


def generate_qr_buffer(content: str) -> io.BytesIO:
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@router.message(Command("admin"))
@router.message(F.text == MAIN_MENU_BTNS["admin"])
async def open_admin_panel(message: Message, db_user: User, db_session: AsyncSession, state: FSMContext = None):
    """Admin dashboard entrance."""
    if state:
        await state.clear()
    if not is_admin(db_user):
        await message.answer("⛔️ شما به این بخش دسترسی ندارید.")
        return

    analytics = AnalyticsService(db_session)
    stats = await analytics.get_admin_dashboard_stats()

    open_t = stats.get('open_tickets', 0)
    ticket_btn_text = f"📩 تیکت‌های پشتیبانی ({open_t})" if open_t > 0 else "📩 تیکت‌های پشتیبانی"

    text = (
        f"⚙️ <b>پنل مدیریت پیشرفته VPN Private</b>\n\n"
        f"📊 <b>آمار کلی سیستم:</b>\n"
        f"👥 کل کاربران: <b>{stats['total_users']} نفر</b>\n"
        f"🟢 سرویس‌های فعال: <b>{stats['active_subscriptions']}</b>\n"
        f"🔴 سرویس‌های منقضی: <b>{stats['expired_subscriptions']}</b>\n"
        f"💵 درآمد امروز: <b>{stats['daily_revenue_toman']:,} تومان</b>\n"
        f"💰 درآمد ماه جاری: <b>{stats['monthly_revenue_toman']:,} تومان</b>\n"
        f"⏳ فیش‌های در انتظار تایید: <b>{stats['pending_payments']} مورد</b>\n"
        f"📩 تیکت‌های باز / در انتظار: <b>{open_t} مورد</b>\n"
        f"⚠️ سفارشات خطا در پنل: <b>{stats['failed_orders']} مورد</b>\n"
        f"🖥 سرورهای آنلاین: <b>{stats['healthy_panels']} از {stats['total_panels']} پنل</b>\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 فیش‌های منتظر تایید", callback_data="admin_view_pending_pays"),
            InlineKeyboardButton(text=ticket_btn_text, callback_data="admin_view_tickets:open:0"),
        ],
        [
            InlineKeyboardButton(text="🖥 سرورها و پنل‌ها", callback_data="admin_view_panels"),
            InlineKeyboardButton(text="📦 مدیریت پلن‌ها", callback_data="admin_manage_plans"),
        ],
        [
            InlineKeyboardButton(text="🌍 مدیریت لوکیشن‌ها", callback_data="admin_manage_locations"),
            InlineKeyboardButton(text="🛠 مدیریت اشتراک‌ها", callback_data="admin_manage_subs"),
        ],
        [
            InlineKeyboardButton(text="👥 لیست کامل کاربران و سرویس‌ها", callback_data="admin_view_all_users:0"),
            InlineKeyboardButton(text="🔍 جستجو و شارژ کاربر", callback_data="admin_prompt_wallet_charge"),
        ],
        [
            InlineKeyboardButton(text="👑 مدیران مجاز", callback_data="admin_manage_admins"),
            InlineKeyboardButton(text="💳 تنظیمات روش‌های پرداخت", callback_data="admin_manage_payments"),
        ],
        [
            InlineKeyboardButton(text="⚙️ تنظیمات ربات و امکانات", callback_data="admin_bot_settings"),
            InlineKeyboardButton(text="🎁 مدیریت اکانت تست", callback_data="admin_manage_trial"),
        ],
        [
            InlineKeyboardButton(text="🎟 کدهای تخفیف", callback_data="admin_manage_coupons"),
            InlineKeyboardButton(text="📢 پیام همگانی هدفمند", callback_data="admin_prompt_broadcast"),
        ],
        [
            InlineKeyboardButton(text="💾 دریافت فایل بکاپ دیتابیس", callback_data="admin_download_db_backup"),
            InlineKeyboardButton(text="🔄 بازخوانی آمار", callback_data="admin_refresh_dashboard"),
        ],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_refresh_dashboard")
async def refresh_admin_dashboard(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    analytics = AnalyticsService(db_session)
    stats = await analytics.get_admin_dashboard_stats()
    open_t = stats.get('open_tickets', 0)
    ticket_btn_text = f"📩 تیکت‌های پشتیبانی ({open_t})" if open_t > 0 else "📩 تیکت‌های پشتیبانی"

    text = (
        f"⚙️ <b>پنل مدیریت پیشرفته VPN Private (به‌روزرسانی شد)</b>\n\n"
        f"📊 <b>آمار کلی سیستم:</b>\n"
        f"👥 کل کاربران: <b>{stats['total_users']} نفر</b>\n"
        f"🟢 سرویس‌های فعال: <b>{stats['active_subscriptions']}</b>\n"
        f"🔴 سرویس‌های منقضی: <b>{stats['expired_subscriptions']}</b>\n"
        f"💵 درآمد امروز: <b>{stats['daily_revenue_toman']:,} تومان</b>\n"
        f"💰 درآمد ماه جاری: <b>{stats['monthly_revenue_toman']:,} تومان</b>\n"
        f"⏳ فیش‌های در انتظار تایید: <b>{stats['pending_payments']} مورد</b>\n"
        f"📩 تیکت‌های باز / در انتظار: <b>{open_t} مورد</b>\n"
        f"⚠️ سفارشات خطا در پنل: <b>{stats['failed_orders']} مورد</b>\n"
        f"🖥 سرورهای آنلاین: <b>{stats['healthy_panels']} از {stats['total_panels']} پنل</b>\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 فیش‌های منتظر تایید", callback_data="admin_view_pending_pays"),
            InlineKeyboardButton(text=ticket_btn_text, callback_data="admin_view_tickets:open:0"),
        ],
        [
            InlineKeyboardButton(text="🖥 سرورها و پنل‌ها", callback_data="admin_view_panels"),
            InlineKeyboardButton(text="📦 مدیریت پلن‌ها", callback_data="admin_manage_plans"),
        ],
        [
            InlineKeyboardButton(text="🌍 مدیریت لوکیشن‌ها", callback_data="admin_manage_locations"),
            InlineKeyboardButton(text="🛠 مدیریت اشتراک‌ها", callback_data="admin_manage_subs"),
        ],
        [
            InlineKeyboardButton(text="👥 لیست کامل کاربران و سرویس‌ها", callback_data="admin_view_all_users:0"),
            InlineKeyboardButton(text="🔍 جستجو و شارژ کاربر", callback_data="admin_prompt_wallet_charge"),
        ],
        [
            InlineKeyboardButton(text="👑 مدیران مجاز", callback_data="admin_manage_admins"),
            InlineKeyboardButton(text="💳 تنظیمات روش‌های پرداخت", callback_data="admin_manage_payments"),
        ],
        [
            InlineKeyboardButton(text="⚙️ تنظیمات ربات و امکانات", callback_data="admin_bot_settings"),
            InlineKeyboardButton(text="🎁 مدیریت اکانت تست", callback_data="admin_manage_trial"),
        ],
        [
            InlineKeyboardButton(text="🎟 کدهای تخفیف", callback_data="admin_manage_coupons"),
            InlineKeyboardButton(text="📢 پیام همگانی هدفمند", callback_data="admin_prompt_broadcast"),
        ],
        [
            InlineKeyboardButton(text="💾 دریافت فایل بکاپ دیتابیس", callback_data="admin_download_db_backup"),
            InlineKeyboardButton(text="🔄 بازخوانی آمار", callback_data="admin_refresh_dashboard"),
        ],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("آمار به‌روزرسانی شد.")


@router.callback_query(F.data == "admin_download_db_backup")
async def download_database_backup(callback: CallbackQuery, db_user: User):
    """Send SQLite database file directly into admin's Telegram chat. Strictly restricted to SuperAdmin."""
    admin_ids = getattr(settings, "ADMIN_TELEGRAM_IDS", [])
    if isinstance(admin_ids, int):
        admin_ids = [admin_ids]
    elif not isinstance(admin_ids, list):
        admin_ids = list(admin_ids)

    is_super = (
        db_user.role in (UserRole.SUPER_ADMIN, "super_admin")
        and (db_user.telegram_id in admin_ids or (admin_ids and db_user.telegram_id == admin_ids[0]))
    )
    if not is_super:
        return await callback.answer(
            "⛔️ دسترسی غیرمجاز! دریافت نسخه پشتیبان دیتابیس منحصراً در اختیار مدیر ارشد (Super Admin) می‌باشد.",
            show_alert=True,
        )

    await callback.answer("در حال تهیه نسخه پشتیبان دیتابیس...")
    import os
    from datetime import datetime

    db_path = "vpn_private.db"
    if not os.path.exists(db_path):
        return await callback.message.answer("❌ فایل پایگاه داده vpn_private.db یافت نشد.")

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    doc = FSInputFile(path=db_path, filename=f"vpn_private_backup_{now_str}.db")

    caption = (
        f"💾 <b>فایل پشتیبان کامل پایگاه داده ربات (SQLite Database)</b>\n\n"
        f"📅 تاریخ تهیه: <code>{datetime.now().strftime('%Y/%m/%d %H:%M:%S')}</code>\n"
        f"📦 حجم فایل: <b>{size_mb:.2f} مگابایت</b>\n"
        f"🔐 وضعیت: کاملاً پایدار، شامل تمام کاربران، سرورها، سفارش‌ها و تنظیمات.\n\n"
        f"✅ <i>شما می‌توانید در صورت نیاز، با جایگزینی این فایل دیتابیس ربات را به این لحظه بازگردانی (Restore) کنید.</i>"
    )
    await callback.message.answer_document(document=doc, caption=caption, parse_mode="HTML")


@router.callback_query(F.data == "admin_view_pending_pays")
async def list_pending_payments(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    receipt_subq = select(PaymentReceipt.payment_id)
    pending_res = await db_session.execute(
        select(Payment)
        .where(
            Payment.status == PaymentStatus.PENDING,
            Payment.provider.in_([PaymentMethod.CARD_TO_CARD, PaymentMethod.CRYPTO, "card_to_card", "crypto"]),
            or_(Payment.id.in_(receipt_subq), Payment.reference_id.is_not(None))
        )
        .order_by(Payment.created_at.desc())
    )
    payments = list(pending_res.scalars().all())

    import html
    if not payments:
        text = "✅ <b>در حال حاضر هیچ فیش واریزی در انتظار تایید وجود ندارد.</b>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        return await callback.answer()

    text = f"⏳ <b>تعداد {len(payments)} فیش در انتظار بررسی:</b>\n\n"
    buttons = []
    for p in payments[:8]:
        user = await db_session.get(User, p.user_id)
        u_name = f"@{html.escape(user.username)}" if user and user.username else str(p.user_id)[:6]
        text += f"• فیش <code>{html.escape(p.tracking_code)}</code> — <b>{p.amount_toman:,} تومان</b> (کاربر: {u_name})\n"
        buttons.append([
            InlineKeyboardButton(text=f"✅ تایید {p.tracking_code}", callback_data=f"admin_pay_approve:{p.id}"),
            InlineKeyboardButton(text=f"❌ رد", callback_data=f"admin_pay_reject:{p.id}"),
        ])

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_view_panels")
async def list_admin_panels(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """List 3x-ui panels, health status, and allow adding, managing, or pinging panels."""
    if not is_admin(db_user):
        return await callback.answer("⛔️ دسترسی غیرمجاز - شما ادمین نیستید.", show_alert=True)

    panels_res = await db_session.execute(select(Panel).options(selectinload(Panel.inbounds)))
    panels = list(panels_res.scalars().all())

    text = "🖥 <b>مدیریت کنترل‌پنل‌های 3x-ui (Multi-Panel):</b>\n\n"
    if not panels:
        text += "<i>در حال حاضر هیچ پنلی در سیستم ثبت نشده است.</i>\n\n"
    else:
        for p in panels:
            status_icon = "🟢 آنلاین" if p.is_healthy else "🔴 قطع"
            active_icon = "✅ فعال" if p.is_active else "⏸ غیرفعال"
            inbounds_cnt = len(p.inbounds) if p.inbounds else 0
            text += f"• <b>{p.name}</b> ➔ {status_icon} | {active_icon}\n"
            text += f"  🔗 آدرس: <code>{p.base_url}</code>\n"
            text += f"  📡 اینباندها: <b>{inbounds_cnt} مورد</b>\n\n"

    keyboard_buttons = []
    if panels:
        for p in panels:
            st = "🟢" if p.is_healthy else "🔴"
            keyboard_buttons.append([
                InlineKeyboardButton(text=f"⚙️ مدیریت پنل: {p.name} ({st})", callback_data=f"admin_panel_manage:{p.id}")
            ])

    keyboard_buttons.append([
        InlineKeyboardButton(text="➕ افزودن پنل 3x-ui جدید", callback_data="admin_prompt_add_panel"),
        InlineKeyboardButton(text="🔄 تست و بررسی اتصال همه پنل‌ها", callback_data="admin_ping_panels"),
    ])
    keyboard_buttons.append([
        InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"edit_text in list_admin_panels failed: {e}")
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_prompt_add_panel")
async def prompt_add_panel(callback: CallbackQuery, state: FSMContext, db_user: User):
    """Prompt admin to start step-by-step 3x-ui panel addition wizard (Step 1: URL)."""
    if not is_admin(db_user):
        return await callback.answer("⛔️ دسترسی غیرمجاز", show_alert=True)

    await state.set_state(AdminState.panel_entering_url)
    text = (
        "➕ <b>افزودن کنترل‌پنل 3x-ui جدید (مرحله ۱ از ۴)</b>\n\n"
        "🌐 لطفاً <b>آدرس کامل پنل به همراه پورت</b> را ارسال فرمایید:\n\n"
        "📌 <i>مثال:</i> <code>http://185.120.40.10:2053</code>\n"
        "(یا اگر دامنه دارید: <code>https://sub.domain.com:2053</code>)\n\n"
        "💡 <i>نکته: وارد کردن پروتکل http یا https الزامی نیست؛ پیش‌فرض http در نظر گرفته می‌شود.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_view_panels")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.panel_entering_url)
@router.message(AdminState.entering_panel_details)
async def process_panel_url(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    """Process URL or full one-liner input."""
    if not is_admin(db_user):
        await state.clear()
        return

    raw_text = (message.text or "").strip()

    from app.integrations.xui.client import ThreeXUIClient, normalize_panel_base_url

    # Support one-liner fallback if admin pasted with | separator
    if "|" in raw_text:
        parts = [p.strip() for p in raw_text.split("|")]
        if len(parts) >= 4:
            name, base_url, username, password = parts[0], parts[1], parts[2], parts[3]
            return await _save_and_test_panel(message, state, db_session, name, base_url, username, password)

    url = normalize_panel_base_url(raw_text)

    await state.update_data(panel_url=url)
    await state.set_state(AdminState.panel_entering_username)

    text = (
        "👤 <b>مرحله ۲ از ۴: نام کاربری (Username)</b>\n\n"
        f"🌐 آدرس سرور ثبت شد: <code>{url}</code>\n\n"
        "حالا لطفاً <b>نام کاربری ورود به پنل 3x-ui</b> را ارسال فرمایید:\n"
        "📌 <i>مثال:</i> <code>admin</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_view_panels")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


process_new_panel = process_panel_url


@router.message(AdminState.panel_entering_username)
async def process_panel_username(message: Message, state: FSMContext, db_user: User):
    """Process panel username (Step 2) and advance to password (Step 3)."""
    if not is_admin(db_user):
        await state.clear()
        return

    username = (message.text or "").strip()
    if not username:
        return await message.answer("⚠️ لطفاً نام کاربری معتبری وارد فرمایید:")

    await state.update_data(panel_username=username)
    await state.set_state(AdminState.panel_entering_password)

    text = (
        "🔑 <b>مرحله ۳ از ۴: کلمه عبور (Password)</b>\n\n"
        f"👤 نام کاربری: <code>{username}</code>\n\n"
        "حالا لطفاً <b>کلمه عبور (پسورد) ورود به پنل 3x-ui</b> را ارسال فرمایید:\n"
        "📌 <i>مثال:</i> <code>admin123</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_view_panels")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(AdminState.panel_entering_password)
async def process_panel_password(message: Message, state: FSMContext, db_user: User):
    """Process panel password (Step 3) and advance to server name (Step 4)."""
    if not is_admin(db_user):
        await state.clear()
        return

    password = (message.text or "").strip()
    if not password:
        return await message.answer("⚠️ لطفاً کلمه عبور را وارد فرمایید:")

    await state.update_data(panel_password=password)
    await state.set_state(AdminState.panel_entering_name)

    text = (
        "🏷 <b>مرحله ۴ از ۴: نام نمایشی سرور / پنل</b>\n\n"
        "مشخصات اتصال و احراز هویت ثبت شد.\n\n"
        "در پایان یک <b>نام دلخواه و مشخص</b> برای این سرور وارد کنید تا در لیست سرورها نمایش داده شود:\n"
        "📌 <i>مثال:</i> <code>سرور آلمان ۲</code> یا <code>فنلاند اختصاصی</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_view_panels")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(AdminState.panel_entering_name)
async def process_panel_name(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    """Process server display name (Step 4) and execute connection test and sync."""
    if not is_admin(db_user):
        await state.clear()
        return

    name = (message.text or "").strip()
    if not name:
        return await message.answer("⚠️ لطفاً نامی برای این سرور وارد فرمایید:")

    data = await state.get_data()
    base_url = data.get("panel_url")
    username = data.get("panel_username")
    password = data.get("panel_password")

    if not base_url or not username or not password:
        await state.clear()
        return await message.answer(
            "⚠️ نشست ثبت پنل ناقص است. لطفاً فرآیند را از ابتدا آغاز فرمایید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ شروع مجدد", callback_data="admin_prompt_add_panel")]
            ])
        )

    await _save_and_test_panel(message, state, db_session, name, base_url, username, password)


async def _save_and_test_panel(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    name: str,
    base_url: str,
    username: str,
    password: str,
):
    """Test connection, authenticate, fetch inbounds, register Server and save Panel."""
    from app.integrations.xui.client import ThreeXUIClient, normalize_panel_base_url
    base_url = normalize_panel_base_url(base_url)

    status_msg = await message.answer(
        "⏳ <b>در حال تست اتصال و احراز هویت با پنل 3x-ui... لطفاً چند ثانیه شکیبا باشید.</b>",
        parse_mode="HTML",
    )

    client = ThreeXUIClient(base_url=base_url, username=username, password=password, timeout=12.0)

    try:
        is_ok = await client.login()
        if not is_ok:
            raise Exception("احراز هویت با نام کاربری یا کلمه عبور وارد شده ناموفق بود.")

        # Fetch remote inbounds from 3x-ui
        inbounds_data = []
        try:
            inbounds_data = await client.get_inbounds()
        except Exception as e:
            logger.warning(f"Could not fetch inbounds: {e}")

        # Check existing or create new panel
        existing = await db_session.execute(
            select(Panel).where((Panel.name == name) | (Panel.base_url == base_url))
        )
        panel = existing.scalar_one_or_none()
        if not panel:
            panel = Panel(
                id=uuid.uuid4(),
                name=name,
                base_url=base_url,
                username=username,
                version="v3.11",
                is_active=True,
                is_healthy=True,
            )
            panel.password = password
            db_session.add(panel)
            await db_session.flush()
        else:
            panel.name = name
            panel.username = username
            panel.password = password
            panel.is_active = True
            panel.is_healthy = True
            panel.error_message = None

        # Synchronize inbounds into DB
        import json
        import html
        saved_inbounds_count = 0
        for inb in inbounds_data:
            rem_id = inb.get("id")
            if rem_id is None:
                continue

            inb_res = await db_session.execute(
                select(Inbound).where(
                    and_(Inbound.panel_id == panel.id, Inbound.remote_inbound_id == rem_id)
                )
            )
            db_inb = inb_res.scalar_one_or_none()

            # Handle stream_settings which can be dict, list, or str from 3x-ui
            stream_raw = inb.get("streamSettings")
            if isinstance(stream_raw, (dict, list)):
                stream_str = json.dumps(stream_raw)
            elif stream_raw is not None:
                stream_str = str(stream_raw)
            else:
                stream_str = ""

            proto_str = str(inb.get("protocol", "vless")).lower()
            port_val = int(inb.get("port", 443))
            listen_val = str(inb.get("listen", "0.0.0.0"))
            tag_val = str(inb.get("tag", ""))
            remark_val = str(inb.get("remark", ""))
            enable_val = bool(inb.get("enable", True))

            if not db_inb:
                db_inb = Inbound(
                    id=uuid.uuid4(),
                    panel_id=panel.id,
                    remote_inbound_id=rem_id,
                    protocol=proto_str,
                    port=port_val,
                    listen=listen_val,
                    tag=tag_val,
                    remark=remark_val,
                    stream_settings=stream_str,
                    is_active=enable_val,
                    max_clients=1000,
                )
                db_session.add(db_inb)
                saved_inbounds_count += 1
            else:
                db_inb.protocol = proto_str
                db_inb.port = port_val
                db_inb.listen = listen_val
                db_inb.tag = tag_val
                db_inb.remark = remark_val
                db_inb.stream_settings = stream_str
                db_inb.is_active = enable_val
                saved_inbounds_count += 1

        # Also register Server entity for subscription link routing
        import urllib.parse
        parsed = urllib.parse.urlparse(base_url)
        sub_host = parsed.hostname or base_url

        srv_res = await db_session.execute(select(Server).where(Server.panel_id == panel.id))
        srv = srv_res.scalar_one_or_none()
        if not srv:
            srv = Server(
                id=uuid.uuid4(),
                name=f"سرور {name}",
                location_code="NODE",
                location_name=name,
                flag_emoji="🌐",
                priority=10,
                panel_id=panel.id,
                subscription_host=sub_host,
                subscription_port=parsed.port or 443,
                is_active=True,
                capacity_limit=1000,
            )
            db_session.add(srv)

        await db_session.commit()
        await state.clear()

        result_text = (
            f"🎉 <b>پنل 3x-ui با موفقیت متصل و ثبت شد!</b>\n\n"
            f"• نام پنل: <b>{panel.name}</b>\n"
            f"• آدرس: <code>{panel.base_url}</code>\n"
            f"• وضعیت: 🟢 <b>آنلاین و پایدار (v3.11)</b>\n"
            f"• تعداد اینباندهای فعال: <b>{saved_inbounds_count} اینباند</b>\n\n"
            f"این پنل هم‌اکنون به سیستم توزیع و فروش خودکار اکانت‌ها افزوده شد."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🖥 مشاهده لیست پنل‌ها", callback_data="admin_view_panels")],
            [InlineKeyboardButton(text="⚙️ بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ])
        try:
            await status_msg.edit_text(result_text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await message.answer(result_text, reply_markup=kb, parse_mode="HTML")

    except Exception as exc:
        logger.error(f"Failed to connect to 3x-ui panel {base_url}: {exc}")
        await state.clear()
        err_detail = str(exc)
        if len(err_detail) > 300:
            err_detail = err_detail[:300] + "..."
        import html
        escaped_err = html.escape(err_detail)
        fail_text = (
            f"❌ <b>خطا در اتصال به پنل 3x-ui:</b>\n\n"
            f"<code>{escaped_err}</code>\n\n"
            f"💡 <b>موارد بررسی:</b>\n"
            f"۱. پورت پنل روی سرور باز باشد (فایروال پورت را نبسته باشد).\n"
            f"۲. آدرس URL، پورت و کلمه عبور دقیقاً درست باشند.\n"
            f"۳. پنل 3x-ui روشن و در دسترس باشد."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 تلاش مجدد", callback_data="admin_prompt_add_panel")],
            [InlineKeyboardButton(text="🔙 بازگشت به پنل‌ها", callback_data="admin_view_panels")],
        ])
        try:
            await status_msg.edit_text(fail_text, reply_markup=kb, parse_mode="HTML")
        except Exception as e_edit:
            logger.warning(f"edit_text failed in _save_and_test_panel: {e_edit}")
            await message.answer(fail_text, reply_markup=kb, parse_mode="HTML")
    finally:
        await client.close()


@router.callback_query(F.data == "admin_ping_panels")
async def ping_all_panels(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Test ping and connection liveness for all configured panels."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    panels_res = await db_session.execute(select(Panel))
    panels = list(panels_res.scalars().all())

    if not panels:
        return await callback.answer("هیچ پنلی جهت تست ثبت نشده است.", show_alert=True)

    await callback.answer("در حال تست اتصال همه پنل‌ها...", show_alert=False)

    from app.integrations.xui.client import ThreeXUIClient
    results = []
    for p in panels:
        client = ThreeXUIClient(
            base_url=p.base_url,
            username=p.username,
            password=p.password,
            api_token=p.api_token,
            login_secret=p.login_secret,
            timeout=6.0,
        )
        try:
            ok = await client.login()
            p.is_healthy = ok
            p.error_message = None if ok else "Login failed"
            status = "🟢 آنلاین" if ok else "🔴 قطع"
        except Exception as e:
            p.is_healthy = False
            p.error_message = str(e)
            status = f"🔴 خطا ({e})"
        finally:
            await client.close()
        results.append(f"• **{p.name}** ➔ {status}")

    await db_session.commit()

    text = "🔄 <b>نتیجه بررسی سلامت و اتصال پنل‌های 3x-ui:</b>\n\n" + "\n".join(results)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن پنل جدید", callback_data="admin_prompt_add_panel")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_view_panels")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_panel_manage:"))
async def manage_single_panel(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Show details and management controls for a single 3x-ui panel."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    panel_id_str = callback.data.split(":")[1]
    panel = await db_session.get(Panel, uuid.UUID(panel_id_str))
    if not panel:
        return await callback.answer("پنل مورد نظر یافت نشد.", show_alert=True)

    inb_res = await db_session.execute(select(func.count(Inbound.id)).where(Inbound.panel_id == panel.id))
    inbounds_count = inb_res.scalar() or 0

    srv_res = await db_session.execute(select(func.count(Server.id)).where(Server.panel_id == panel.id))
    servers_count = srv_res.scalar() or 0

    subs_res = await db_session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.panel_id == panel.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )
    active_subs = subs_res.scalar() or 0

    status_icon = "🟢 آنلاین و پایدار" if panel.is_healthy else "🔴 قطع ارتباط"
    active_status = "✅ فعال (آماده توزیع اکانت)" if panel.is_active else "⏸ غیرفعال (توزیع متوقف)"
    sync_time = panel.last_sync_at.strftime("%Y/%m/%d %H:%M") if panel.last_sync_at else "ثبت نشده"

    text = (
        f"⚙️ <b>مدیریت کنترل‌پنل 3X-UI: «{panel.name}»</b>\n\n"
        f"• <b>وضعیت اتصال:</b> {status_icon}\n"
        f"• <b>وضعیت توزیع:</b> {active_status}\n"
        f"• <b>آدرس وب:</b> <code>{panel.base_url}</code>\n"
        f"• <b>نام کاربری:</b> <code>{panel.username}</code>\n"
        f"• <b>نسخه:</b> <code>{panel.version}</code>\n"
        f"• <b>اینباندهای فعال:</b> <b>{inbounds_count} عدد</b>\n"
        f"• <b>سرورها/نودها:</b> <b>{servers_count} نود</b>\n"
        f"• <b>اشتراک‌های فعال متصل:</b> <b>{active_subs} کاربر</b>\n"
        f"• <b>آخرین همگام‌سازی:</b> <code>{sync_time}</code>\n"
    )
    if panel.error_message:
        text += f"\n⚠️ <b>آخرین خطا:</b> <code>{panel.error_message[:150]}</code>\n"

    toggle_btn_text = "⏸ غیرفعال‌سازی پنل" if panel.is_active else "▶️ فعال‌سازی پنل"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 همگام‌سازی اینباندها", callback_data=f"admin_panel_sync:{panel.id}"),
            InlineKeyboardButton(text="⚡️ تست اتصال و پینگ", callback_data=f"admin_panel_test:{panel.id}"),
        ],
        [
            InlineKeyboardButton(text=toggle_btn_text, callback_data=f"admin_panel_toggle:{panel.id}"),
            InlineKeyboardButton(text="🗑 حذف پنل", callback_data=f"admin_panel_delete:{panel.id}"),
        ],
        [
            InlineKeyboardButton(text="🔙 بازگشت به لیست پنل‌ها", callback_data="admin_view_panels"),
        ],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_panel_sync:"))
async def sync_single_panel(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Re-sync inbounds from remote 3x-ui for this panel."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    panel_id_str = callback.data.split(":")[1]
    panel = await db_session.get(Panel, uuid.UUID(panel_id_str))
    if not panel:
        return await callback.answer("پنل یافت نشد.", show_alert=True)

    await callback.answer("در حال دریافت اینباندها از 3x-ui...")

    from app.integrations.xui.client import ThreeXUIClient
    client = ThreeXUIClient(
        base_url=panel.base_url,
        username=panel.username,
        password=panel.password,
        api_token=panel.api_token,
        login_secret=panel.login_secret,
        timeout=12.0,
    )
    try:
        ok = await client.login()
        if not ok:
            panel.is_healthy = False
            panel.error_message = "احراز هویت ناموفق بود."
            await db_session.commit()
            return await callback.message.answer(f"❌ ورود به پنل {panel.name} ناموفق بود. لطفاً رمز عبور را بررسی نمایید.")

        inbounds_data = await client.get_inbounds()
        panel.is_healthy = True
        panel.error_message = None
        panel.last_sync_at = datetime.now(timezone.utc)

        synced_count = 0
        for inb in inbounds_data:
            rem_id = inb.get("id")
            if rem_id is None:
                continue

            inb_res = await db_session.execute(
                select(Inbound).where(
                    and_(Inbound.panel_id == panel.id, Inbound.remote_inbound_id == rem_id)
                )
            )
            db_inb = inb_res.scalar_one_or_none()

            import json
            stream_raw = inb.get("streamSettings")
            stream_str = json.dumps(stream_raw) if isinstance(stream_raw, (dict, list)) else (str(stream_raw) if stream_raw else "")
            proto_str = str(inb.get("protocol", "vless")).lower()
            port_val = int(inb.get("port", 443))
            listen_val = str(inb.get("listen", "0.0.0.0"))
            tag_val = str(inb.get("tag", ""))
            remark_val = str(inb.get("remark", ""))
            enable_val = bool(inb.get("enable", True))

            if not db_inb:
                db_inb = Inbound(
                    id=uuid.uuid4(),
                    panel_id=panel.id,
                    remote_inbound_id=rem_id,
                    protocol=proto_str,
                    port=port_val,
                    listen=listen_val,
                    tag=tag_val,
                    remark=remark_val,
                    stream_settings=stream_str,
                    is_active=enable_val,
                    max_clients=1000,
                )
                db_session.add(db_inb)
            else:
                db_inb.protocol = proto_str
                db_inb.port = port_val
                db_inb.listen = listen_val
                db_inb.tag = tag_val
                db_inb.remark = remark_val
                db_inb.stream_settings = stream_str
                db_inb.is_active = enable_val
            synced_count += 1

        await db_session.commit()
        await callback.message.answer(f"✅ همگام‌سازی پنل «{panel.name}» با موفقیت انجام شد ({synced_count} اینباند ثبت شد).")
        return await manage_single_panel(callback, db_user, db_session)
    except Exception as exc:
        panel.is_healthy = False
        panel.error_message = str(exc)
        await db_session.commit()
        return await callback.message.answer(f"❌ خطا در همگام‌سازی پنل {panel.name}: {exc}")
    finally:
        await client.close()


@router.callback_query(F.data.startswith("admin_panel_test:"))
async def test_single_panel(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Test ping and connection to a single panel."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    panel_id_str = callback.data.split(":")[1]
    panel = await db_session.get(Panel, uuid.UUID(panel_id_str))
    if not panel:
        return await callback.answer("پنل یافت نشد.", show_alert=True)

    await callback.answer("در حال تست اتصال...")
    import time
    t0 = time.perf_counter()
    from app.integrations.xui.client import ThreeXUIClient
    client = ThreeXUIClient(
        base_url=panel.base_url,
        username=panel.username,
        password=panel.password,
        api_token=panel.api_token,
        login_secret=panel.login_secret,
        timeout=10.0,
    )
    try:
        ok = await client.login()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        panel.is_healthy = ok
        panel.error_message = None if ok else "احراز هویت ناموفق"
        panel.last_sync_at = datetime.now(timezone.utc)
        await db_session.commit()

        if ok:
            inbs = await client.get_inbounds()
            await callback.answer(f"🟢 اتصال موفق! پینگ: {ms}ms | اینباندها: {len(inbs)}", show_alert=True)
        else:
            await callback.answer(f"🔴 نام کاربری یا رمز عبور پنل اشتباه است.", show_alert=True)
    except Exception as exc:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        panel.is_healthy = False
        panel.error_message = str(exc)
        await db_session.commit()
        await callback.answer(f"🔴 خطا در اتصال ({ms}ms): {str(exc)[:80]}", show_alert=True)
    finally:
        await client.close()

    return await manage_single_panel(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_panel_toggle:"))
async def toggle_single_panel(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Toggle panel active/inactive state."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    panel_id_str = callback.data.split(":")[1]
    panel = await db_session.get(Panel, uuid.UUID(panel_id_str))
    if not panel:
        return await callback.answer("پنل یافت نشد.", show_alert=True)

    panel.is_active = not panel.is_active
    await db_session.commit()
    msg = f"پنل «{panel.name}» " + ("فعال شد." if panel.is_active else "غیرفعال گردید.")
    await callback.answer(msg, show_alert=True)
    return await manage_single_panel(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_panel_delete:"))
async def delete_single_panel(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Delete panel with active subscription validation."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    panel_id_str = callback.data.split(":")[1]
    panel = await db_session.get(Panel, uuid.UUID(panel_id_str))
    if not panel:
        return await callback.answer("پنل یافت نشد.", show_alert=True)

    active_subs = (
        await db_session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.panel_id == panel.id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
        )
    ).scalar() or 0

    if active_subs > 0:
        return await callback.answer(
            f"❌ امکان حذف وجود ندارد! تعداد {active_subs} اشتراک فعال روی این پنل تعریف شده است.",
            show_alert=True,
        )

    # Delete related servers and inbounds
    servers = (await db_session.execute(select(Server).where(Server.panel_id == panel.id))).scalars().all()
    for s in servers:
        await db_session.delete(s)

    inbounds = (await db_session.execute(select(Inbound).where(Inbound.panel_id == panel.id))).scalars().all()
    for ib in inbounds:
        await db_session.delete(ib)

    panel_name = panel.name
    await db_session.delete(panel)
    await db_session.commit()

    await callback.answer(f"✅ پنل «{panel_name}» با موفقیت حذف شد.", show_alert=True)
    return await list_admin_panels(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_pay_approve:"))
async def approve_payment_by_admin(
    callback: CallbackQuery,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    """Admin approves pending Card-to-Card receipt."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی ندارید.", show_alert=True)

    payment_id = uuid.UUID(callback.data.split(":")[1])
    payment = await db_session.get(Payment, payment_id)
    if not payment:
        return await callback.answer("پرداخت یافت نشد.", show_alert=True)
    if payment.status == PaymentStatus.PAID:
        return await callback.answer("این پرداخت قبلاً تایید شده است.", show_alert=True)
    if payment.provider not in (PaymentMethod.CARD_TO_CARD, PaymentMethod.CRYPTO, "card_to_card", "crypto"):
        return await callback.answer("فقط پرداخت‌های دستی (کارت‌به‌کارت و کریپتو) قابل تایید هستند.", show_alert=True)

    receipt_res = await db_session.execute(select(PaymentReceipt).where(PaymentReceipt.payment_id == payment.id))
    receipt = receipt_res.scalars().first()
    if not receipt and not payment.reference_id and not payment.order_id:
        return await callback.answer("برای این سفارش هنوز فیش یا اطلاعات واریزی ثبت نشده است.", show_alert=True)

    order_service = OrderService(db_session)
    sub_service = SubscriptionService(db_session)

    try:
        subscription = await order_service.approve_and_provision_order(
            payment_id=payment_id,
            admin_id=callback.from_user.id,
            reference_id="APPROVED_BY_ADMIN",
        )
        import html
        admin_first_name = html.escape(callback.from_user.first_name or "")
        ack_text = f"\n\n✅ <b>توسط مدیریت ({admin_first_name}) تایید و سرویس فعال شد.</b>"

        if callback.message.caption is not None:
            await callback.message.edit_caption(
                caption=f"{callback.message.caption}{ack_text}",
                reply_markup=None,
                parse_mode="HTML",
            )
        elif callback.message.text is not None:
            await callback.message.edit_text(
                text=f"{callback.message.text}{ack_text}",
                reply_markup=None,
                parse_mode="HTML",
            )

        payment = await db_session.get(Payment, payment_id)
        customer = await db_session.get(User, payment.user_id) if payment else None

        if subscription:
            # Notify the customer directly with subscription and QR code
            if customer:
                conn = await sub_service.get_connection_details(subscription.id)
                qr_buf = generate_qr_buffer(conn["subscription_url"])
                qr_file = BufferedInputFile(qr_buf.getvalue(), filename="sub_qr.png")

                from app.modules.bot_texts.service import get_bot_text
                from app.core.config import settings

                support_u = getattr(settings, "SUPPORT_USERNAME", "VPNPrivateSupport").lstrip("@")
                delivery_template = get_bot_text("order_approved_delivery")

                class SafeDict(dict):
                    def __missing__(self, key):
                        return f"{{{key}}}"

                customer_msg = delivery_template.format_map(
                    SafeDict(
                        client_email=subscription.client_email or "",
                        location=conn.get("location", ""),
                        flag=conn.get("flag", ""),
                        protocol=conn.get("protocol", ""),
                        subscription_url=conn.get("subscription_url", ""),
                        support_username=support_u,
                        remark=conn.get("remark", ""),
                    )
                )
                try:
                    await bot.send_photo(
                        chat_id=customer.telegram_id,
                        photo=qr_file,
                        caption=customer_msg,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Failed to send sub to customer {customer.telegram_id}: {e}")
        else:
            if customer and payment:
                try:
                    await bot.send_message(
                        chat_id=customer.telegram_id,
                        text=(
                            f"🎉 <b>شارژ کیف پول شما تایید شد!</b>\n\n"
                            f"💰 <b>مبلغ افزوده شده:</b> <b>{payment.amount_toman:,} تومان</b>\n"
                            f"✅ موجودی کیف پول شما با موفقیت افزایش یافت."
                        ),
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error(f"Failed to notify customer {customer.telegram_id} for wallet top-up: {e}")

        # Broadcast approval outcome to all other admins
        try:
            admin_username = f" (@{html.escape(callback.from_user.username)})" if callback.from_user.username else ""
            c_name = html.escape(customer.first_name or "کاربر") if customer else "کاربر"
            c_user = f" (@{html.escape(customer.username)})" if (customer and customer.username) else ""
            c_tg = customer.telegram_id if customer else "—"

            track_code = payment.tracking_code if payment else "—"
            amount_t = f"{payment.amount_toman:,} تومان" if payment else "—"
            sub_email = subscription.client_email if subscription else "شارژ کیف پول"

            broadcast_msg = (
                f"✅ <b>#گزارش_فروش: تایید سفارش و فعال‌سازی</b>\n\n"
                f"👤 <b>مشتری:</b> {c_name}{c_user}\n"
                f"🆔 <b>شناسه مشتری:</b> <code>{c_tg}</code>\n"
                f"💰 <b>مبلغ:</b> <b>{amount_t}</b>\n"
                f"🔖 <b>کد پیگیری:</b> <code>{track_code}</code>\n"
                f"🏷 <b>شناسه اکانت:</b> <code>{sub_email}</code>\n"
                f"👑 <b>تایید شده توسط:</b> <b>{admin_first_name}</b>{admin_username}\n"
                f"🚀 <b>وضعیت:</b> سرویس در پنل فعال و مشخصات به خریدار تحویل گردید."
            )
            await notify_all_admins(
                bot=bot,
                db_session=db_session,
                text=broadcast_msg,
                exclude_telegram_id=callback.from_user.id,
            )
        except Exception as e_bc:
            logger.warning(f"Failed to broadcast approval to other admins: {e_bc}")

        await callback.answer("پرداخت با موفقیت تایید و سرویس کاربر ایجاد شد.")

    except Exception as e:
        logger.error(f"Error approving payment {payment_id}: {e}", exc_info=True)
        await db_session.rollback()
        await callback.answer(f"خطا در ایجاد سرویس در پنل: {e}", show_alert=True)


@router.callback_query(F.data.startswith("admin_pay_reject:"))
async def reject_payment_by_admin(
    callback: CallbackQuery,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    """Admin rejects pending receipt."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی ندارید.", show_alert=True)

    payment_id = uuid.UUID(callback.data.split(":")[1])
    payment = await db_session.get(Payment, payment_id)
    if not payment:
        return await callback.answer("پرداخت یافت نشد.", show_alert=True)

    if payment.status == PaymentStatus.PAID:
        return await callback.answer(
            "⚠️ این پرداخت قبلاً تایید شده و سرویس کاربر فعال گردیده است!\nامکان رد کردن پرداخت تاییدشده وجود ندارد.",
            show_alert=True,
        )

    if payment:
        payment.status = PaymentStatus.REJECTED
        payment.admin_id = callback.from_user.id
        await db_session.flush()

        customer = await db_session.get(User, payment.user_id)
        if customer:
            try:
                await bot.send_message(
                    chat_id=customer.telegram_id,
                    text="❌ <b>فیش واریزی ارسالی شما توسط مدیریت رد شد.</b>\nدر صورت وجود هرگونه مغایرت، لطفاً به بخش پشتیبانی پیام ارسال فرمایید.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        # Broadcast rejection outcome to all other admins
        try:
            import html
            admin_first_name = html.escape(callback.from_user.first_name or "مدیریت")
            admin_username = f" (@{html.escape(callback.from_user.username)})" if callback.from_user.username else ""
            c_name = html.escape(customer.first_name or "کاربر") if customer else "کاربر"
            c_user = f" (@{html.escape(customer.username)})" if (customer and customer.username) else ""
            c_tg = customer.telegram_id if customer else "—"
            track_code = payment.tracking_code if payment else "—"
            amount_t = f"{payment.amount_toman:,} تومان" if payment else "—"

            broadcast_msg = (
                f"❌ <b>#گزارش_فروش: فیش واریزی رد شد (تکذیب)</b>\n\n"
                f"👤 <b>مشتری:</b> {c_name}{c_user}\n"
                f"🆔 <b>شناسه مشتری:</b> <code>{c_tg}</code>\n"
                f"💰 <b>مبلغ:</b> <b>{amount_t}</b>\n"
                f"🔖 <b>کد پیگیری:</b> <code>{track_code}</code>\n"
                f"👑 <b>رد شده توسط:</b> <b>{admin_first_name}</b>{admin_username}\n"
                f"⚠️ <b>وضعیت:</b> پرداخت لغو گردید و پیام عدم تایید به مشتری ارسال شد."
            )
            await notify_all_admins(
                bot=bot,
                db_session=db_session,
                text=broadcast_msg,
                exclude_telegram_id=callback.from_user.id,
            )
        except Exception as e_bc:
            logger.warning(f"Failed to broadcast rejection to other admins: {e_bc}")

    rej_text = "\n\n❌ <b>توسط مدیریت رد شد.</b>"
    if callback.message.caption is not None:
        await callback.message.edit_caption(
            caption=f"{callback.message.caption}{rej_text}",
            reply_markup=None,
            parse_mode="HTML",
        )
    elif callback.message.text is not None:
        await callback.message.edit_text(
            text=f"{callback.message.text}{rej_text}",
            reply_markup=None,
            parse_mode="HTML",
        )
    await callback.answer("پرداخت رد شد.")


@router.callback_query(F.data == "admin_manage_plans")
async def list_admin_plans(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """List all products/plans for admin inspection and pricing updates."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    products_res = await db_session.execute(select(Product).order_by(Product.sort_order.asc()))
    products = list(products_res.scalars().all())

    text = (
        "📦 <b>مدیریت پلن‌ها و قیمت‌های فروش:</b>\n\n"
        "جهت مشاهده جزئیات، ویرایش نام، حجم، مدت و قیمت هر پلن روی آن کلیک کنید:\n"
    )
    buttons = [
        [InlineKeyboardButton(text="➕ افزودن پلن جدید", callback_data="admin_add_plan_start")]
    ]
    for p in products:
        status_dot = "🟢" if p.is_active else "🔴"
        btn_text = f"{status_dot} {p.name} — {p.price_toman:,} ت"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin_prod_detail:{p.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")])
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_prod_detail:"))
async def view_admin_product_detail(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """View detailed parameters of a product and manage price or status."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    prod_id = uuid.UUID(callback.data.split(":")[1])
    product = await db_session.get(Product, prod_id)
    if not product:
        return await callback.answer("پلن مورد نظر یافت نشد.", show_alert=True)

    status_str = "🟢 فعال و در حال فروش" if product.is_active else "🔴 غیرفعال (مخفی از لیست فروش)"
    toggle_text = "🔴 غیرفعال‌سازی این پلن" if product.is_active else "🟢 فعال‌سازی این پلن"

    text = (
        f"⚙️ <b>مدیریت پلن:</b> <code>{product.name}</code>\n\n"
        f"📊 حجم ترافیک: <b>{product.traffic_display}</b>\n"
        f"⏳ مدت اعتبار: <b>{product.duration_days} روز</b>\n"
        f"💰 قیمت فعلی: <b>{product.price_toman:,} تومان</b> ({product.price_irr:,} ریال)\n"
        f"📡 پروتکل پیش‌فرض: <b>{product.protocol.upper()}</b>\n"
        f"وضعیت فروش: <b>{status_str}</b>\n\n"
        f"جهت ویرایش هر بخش یا حذف پلن، از گزینه‌های زیر استفاده نمایید:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ تغییر نام پلن", callback_data=f"admin_edit_prod_name:{product.id}"),
            InlineKeyboardButton(text="💰 تغییر قیمت", callback_data=f"admin_edit_price:{product.id}"),
        ],
        [
            InlineKeyboardButton(text="📊 تغییر حجم ترافیک", callback_data=f"admin_edit_prod_traffic:{product.id}"),
            InlineKeyboardButton(text="⏳ تغییر مدت زمان (روز)", callback_data=f"admin_edit_prod_duration:{product.id}"),
        ],
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"admin_toggle_prod:{product.id}"),
            InlineKeyboardButton(text="🗑 حذف پلن", callback_data=f"admin_delete_prod:{product.id}"),
        ],
        [
            InlineKeyboardButton(text="🔙 بازگشت به لیست پلن‌ها", callback_data="admin_manage_plans"),
        ],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_toggle_prod:"))
async def toggle_product_status(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Toggle a plan between active and inactive in the customer catalog."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    prod_id = uuid.UUID(callback.data.split(":")[1])
    product = await db_session.get(Product, prod_id)
    if not product:
        return await callback.answer("پلن یافت نشد.", show_alert=True)

    product.is_active = not product.is_active
    await db_session.commit()

    action_label = "فعال شد و در ربات به نمایش درآمد" if product.is_active else "غیرفعال شد و از منوی خرید مخفی گردید"
    await callback.answer(f"پلن با موفقیت {action_label}.", show_alert=True)
    await view_admin_product_detail(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_edit_price:"))
async def prompt_edit_product_price(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    """Prompt admin to enter new price in Toman for a specific plan."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    prod_id = uuid.UUID(callback.data.split(":")[1])
    product = await db_session.get(Product, prod_id)
    if not product:
        return await callback.answer("پلن یافت نشد.", show_alert=True)

    await state.set_state(AdminState.editing_product_price)
    await state.update_data(editing_product_id=str(product.id))

    text = (
        f"✏️ <b>ویرایش قیمت پلن:</b> <code>{product.name}</code>\n\n"
        f"💰 قیمت فعلی: <b>{product.price_toman:,} تومان</b>\n\n"
        f"لطفاً <b>قیمت جدید را به تومان</b> ارسال فرمایید:\n"
        f"(صرفاً عدد لاتین یا فارسی، مثلاً: <code>150000</code> یا <code>۲۵۰۰۰۰</code>)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"admin_prod_detail:{product.id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_product_price)
async def process_new_product_price(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    """Process admin input for new price, validate, convert, and update database."""
    if not is_admin(db_user):
        await state.clear()
        return

    data = await state.get_data()
    prod_id_str = data.get("editing_product_id")
    if not prod_id_str:
        await state.clear()
        return await message.answer("⚠️ نشست ویرایش نامعتبر است.")

    product = await db_session.get(Product, uuid.UUID(prod_id_str))
    if not product:
        await state.clear()
        return await message.answer("⚠️ پلن مورد نظر در دیتابیس یافت نشد.")

    raw_text = (message.text or "").strip().replace(",", "").replace("،", "").replace(" ", "")
    clean_digits = raw_text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))

    if not clean_digits.isdigit() or int(clean_digits) <= 0:
        return await message.answer(
            "⚠️ لطفاً یک رقم عددی معتبر و بزرگتر از صفر به تومان وارد فرمایید (مثال: <code>180000</code>):",
            parse_mode="HTML"
        )

    new_price_toman = int(clean_digits)
    new_price_irr = new_price_toman * 10

    old_price_toman = product.price_toman
    product.price_irr = new_price_irr
    await db_session.commit()
    await state.clear()

    text = (
        f"✅ <b>قیمت پلن «{product.name}» با موفقیت تغییر یافت.</b>\n\n"
        f"• قیمت قبلی: <b>{old_price_toman:,} تومان</b>\n"
        f"• قیمت جدید: <b>{new_price_toman:,} تومان</b> ({new_price_irr:,} ریال)\n\n"
        f"این قیمت بلافاصله در منوی خرید تمامی مشتریان اعمال گردید."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ مشاهده و مدیریت پلن", callback_data=f"admin_prod_detail:{product.id}")],
        [InlineKeyboardButton(text="📦 لیست تمام پلن‌ها", callback_data="admin_manage_plans")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_edit_prod_name:"))
async def prompt_edit_product_name(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    prod_id = uuid.UUID(callback.data.split(":")[1])
    product = await db_session.get(Product, prod_id)
    if not product:
        return await callback.answer("پلن یافت نشد.", show_alert=True)
    await state.set_state(AdminState.editing_product_name)
    await state.update_data(editing_product_id=str(product.id))
    text = (
        f"✏️ <b>ویرایش نام پلن:</b>\n\n"
        f"نام فعلی: <code>{product.name}</code>\n\n"
        f"لطفاً <b>نام جدید پلن</b> را ارسال فرمایید (مثال: <code>پلن ۱۰۰ گیگابایت VIP</code>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"admin_prod_detail:{product.id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_product_name)
async def process_edit_product_name(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        await state.clear()
        return
    data = await state.get_data()
    prod_id_str = data.get("editing_product_id")
    product = await db_session.get(Product, uuid.UUID(prod_id_str)) if prod_id_str else None
    if not product:
        await state.clear()
        return await message.answer("⚠️ پلن مورد نظر یافت نشد.")
    new_name = (message.text or "").strip()
    if not new_name or len(new_name) < 2:
        return await message.answer("⚠️ نام پلن باید حداقل ۲ کاراکتر باشد:")
    product.name = new_name
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>نام پلن با موفقیت به «{new_name}» تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ مشاهده پلن", callback_data=f"admin_prod_detail:{product.id}")],
            [InlineKeyboardButton(text="📦 لیست پلن‌ها", callback_data="admin_manage_plans")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("admin_edit_prod_traffic:"))
async def prompt_edit_product_traffic(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    prod_id = uuid.UUID(callback.data.split(":")[1])
    product = await db_session.get(Product, prod_id)
    if not product:
        return await callback.answer("پلن یافت نشد.", show_alert=True)
    await state.set_state(AdminState.editing_product_traffic)
    await state.update_data(editing_product_id=str(product.id))
    text = (
        f"📊 <b>ویرایش حجم ترافیک:</b> <code>{product.name}</code>\n\n"
        f"حجم فعلی: <b>{product.traffic_display}</b>\n\n"
        f"لطفاً <b>حجم جدید را به گیگابایت</b> وارد فرمایید:\n"
        f"(عدد لاتین یا فارسی، برای نامحدود عدد <code>0</code> را ارسال نمایید)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"admin_prod_detail:{product.id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_product_traffic)
async def process_edit_product_traffic(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        await state.clear()
        return
    data = await state.get_data()
    prod_id_str = data.get("editing_product_id")
    product = await db_session.get(Product, uuid.UUID(prod_id_str)) if prod_id_str else None
    if not product:
        await state.clear()
        return await message.answer("⚠️ پلن مورد نظر یافت نشد.")
    raw_text = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
    if not raw_text.isdigit():
        return await message.answer("⚠️ لطفاً یک رقم عددی معتبر (مثال: 50 یا 0 برای نامحدود) وارد فرمایید:")
    gb_val = int(raw_text)
    product.traffic_bytes = gb_val * (1024 ** 3)
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>حجم ترافیک پلن با موفقیت به {product.traffic_display} تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ مشاهده پلن", callback_data=f"admin_prod_detail:{product.id}")],
            [InlineKeyboardButton(text="📦 لیست پلن‌ها", callback_data="admin_manage_plans")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("admin_edit_prod_duration:"))
async def prompt_edit_product_duration(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    prod_id = uuid.UUID(callback.data.split(":")[1])
    product = await db_session.get(Product, prod_id)
    if not product:
        return await callback.answer("پلن یافت نشد.", show_alert=True)
    await state.set_state(AdminState.editing_product_duration)
    await state.update_data(editing_product_id=str(product.id))
    text = (
        f"⏳ <b>ویرایش مدت اعتبار:</b> <code>{product.name}</code>\n\n"
        f"مدت فعلی: <b>{product.duration_days} روز</b>\n\n"
        f"لطفاً <b>مدت اعتبار جدید را به روز</b> ارسال فرمایید (مثال: <code>30</code> یا <code>60</code>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"admin_prod_detail:{product.id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_product_duration)
async def process_edit_product_duration(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        await state.clear()
        return
    data = await state.get_data()
    prod_id_str = data.get("editing_product_id")
    product = await db_session.get(Product, uuid.UUID(prod_id_str)) if prod_id_str else None
    if not product:
        await state.clear()
        return await message.answer("⚠️ پلن مورد نظر یافت نشد.")
    raw_text = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
    if not raw_text.isdigit() or int(raw_text) <= 0:
        return await message.answer("⚠️ لطفاً یک عدد معتبر بزرگتر از صفر (مثال: 30) وارد فرمایید:")
    product.duration_days = int(raw_text)
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>مدت اعتبار پلن با موفقیت به {product.duration_days} روز تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ مشاهده پلن", callback_data=f"admin_prod_detail:{product.id}")],
            [InlineKeyboardButton(text="📦 لیست پلن‌ها", callback_data="admin_manage_plans")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("admin_delete_prod:"))
async def prompt_delete_product(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    prod_id = uuid.UUID(callback.data.split(":")[1])
    product = await db_session.get(Product, prod_id)
    if not product:
        return await callback.answer("پلن یافت نشد.", show_alert=True)
    text = (
        f"⚠️ <b>تایید حذف پلن:</b>\n\n"
        f"آیا اطمینان دارید که می‌خواهید پلن <code>{product.name}</code> را به طور کامل حذف نمایید؟\n\n"
        f"<i>توجه: حذف پلن تاثیری بر اشتراک‌های فعلی کاربران که قبلاً خریداری شده ندارد.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 بله، حذف شود", callback_data=f"admin_confirm_delete_prod:{product.id}"),
            InlineKeyboardButton(text="❌ انصراف", callback_data=f"admin_prod_detail:{product.id}"),
        ]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_confirm_delete_prod:"))
async def confirm_delete_product(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    prod_id = uuid.UUID(callback.data.split(":")[1])
    product = await db_session.get(Product, prod_id)
    if product:
        await db_session.delete(product)
        await db_session.commit()
    await callback.answer("پلن با موفقیت حذف گردید.", show_alert=True)
    return await list_admin_plans(callback, db_user, db_session)


@router.callback_query(F.data == "admin_add_plan_start")
async def start_add_plan(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.plan_entering_name)
    text = (
        "➕ <b>افزودن پلن جدید — مرحله ۱ از ۴:</b>\n\n"
        "لطفاً <b>نام پلن</b> را ارسال نمایید:\n\n"
        "💡 مثال: <code>پلن ۵۰ گیگابایت یک‌ماهه</code> یا <code>پلن نامحدود ۳ لوکیشن</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_plans")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.plan_entering_name)
async def process_plan_name(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    name = (message.text or "").strip()
    if len(name) < 2:
        return await message.answer("⚠️ لطفاً نامی با حداقل ۲ کاراکتر ارسال فرمایید:")
    await state.update_data(new_plan_name=name)
    await state.set_state(AdminState.plan_entering_traffic)
    await message.answer(
        f"📊 <b>مرحله ۲ از ۴ (حجم ترافیک):</b>\n\n"
        f"پلن: <b>{name}</b>\n\n"
        f"لطفاً <b>حجم ترافیک را به گیگابایت</b> وارد کنید:\n"
        f"(مثال: <code>50</code> یا <code>100</code> یا عدد <code>0</code> برای نامحدود)",
        parse_mode="HTML"
    )


@router.message(AdminState.plan_entering_traffic)
async def process_plan_traffic(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    clean_digits = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
    if not clean_digits.isdigit():
        return await message.answer("⚠️ لطفاً یک رقم عددی معتبر (مثال: 50 یا 0) وارد فرمایید:")
    gb_val = int(clean_digits)
    await state.update_data(new_plan_gb=gb_val)
    await state.set_state(AdminState.plan_entering_duration)
    await message.answer(
        f"⏳ <b>مرحله ۳ از ۴ (مدت زمان):</b>\n\n"
        f"لطفاً <b>مدت اعتبار پلن را به روز</b> وارد فرمایید (مثال: <code>30</code> برای یک ماه):",
        parse_mode="HTML"
    )


@router.message(AdminState.plan_entering_duration)
async def process_plan_duration(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    clean_digits = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
    if not clean_digits.isdigit() or int(clean_digits) <= 0:
        return await message.answer("⚠️ لطفاً یک عدد معتبر بزرگتر از صفر (مثال: 30) وارد فرمایید:")
    await state.update_data(new_plan_duration=int(clean_digits))
    await state.set_state(AdminState.plan_entering_price)
    await message.answer(
        f"💰 <b>مرحله ۴ از ۴ (قیمت فروش):</b>\n\n"
        f"لطفاً <b>قیمت پلن را به تومان</b> وارد فرمایید (مثال: <code>150000</code>):",
        parse_mode="HTML"
    )


@router.message(AdminState.plan_entering_price)
async def process_plan_price(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return
    clean_digits = (message.text or "").strip().replace(",", "").replace("،", "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
    if not clean_digits.isdigit() or int(clean_digits) <= 0:
        return await message.answer("⚠️ لطفاً رقم قیمت معتبر و بزرگتر از صفر به تومان وارد فرمایید (مثال: 150000):")
    price_toman = int(clean_digits)
    price_irr = price_toman * 10

    data = await state.get_data()
    name = data.get("new_plan_name", "پلن جدید")
    gb_val = data.get("new_plan_gb", 50)
    duration = data.get("new_plan_duration", 30)
    traffic_bytes = gb_val * (1024 ** 3) if gb_val > 0 else 0

    new_prod = Product(
        name=name,
        traffic_bytes=traffic_bytes,
        duration_days=duration,
        price_irr=price_irr,
        protocol="vless",
        is_active=True,
    )
    db_session.add(new_prod)
    await db_session.commit()
    await state.clear()

    text = (
        f"✅ <b>پلن جدید با موفقیت ایجاد شد و در ربات فعال گردید!</b>\n\n"
        f"🏷 نام: <b>{new_prod.name}</b>\n"
        f"📊 حجم: <b>{new_prod.traffic_display}</b>\n"
        f"⏳ مدت: <b>{new_prod.duration_days} روز</b>\n"
        f"💰 قیمت: <b>{price_toman:,} تومان</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ مشاهده و مدیریت پلن", callback_data=f"admin_prod_detail:{new_prod.id}")],
        [InlineKeyboardButton(text="📦 لیست تمام پلن‌ها", callback_data="admin_manage_plans")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ==============================================================================
# LOCATION & SERVER MANAGEMENT (روشن/خاموش کردن لوکیشن‌ها و افزودن سرور)
# ==============================================================================

@router.callback_query(F.data == "admin_manage_locations")
async def list_admin_locations(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """List all servers/locations, showing their on/off toggle and details."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    servers_res = await db_session.execute(select(Server).order_by(Server.priority.desc(), Server.created_at.asc()))
    servers = list(servers_res.scalars().all())

    text = (
        "🌍 <b>مدیریت لوکیشن‌ها و سرورها:</b>\n\n"
        "در این بخش می‌توانید لوکیشن‌های جدید اضافه کنید یا هر لوکیشن را روشن/خاموش نمایید.\n"
        "لوکیشن‌های <b>🟢 روشن</b> در منوی خرید مشتریان نمایش داده می‌شوند و لوکیشن‌های <b>🔴 خاموش</b> مخفی هستند.\n\n"
        "👇 <b>لیست لوکیشن‌های ثبت‌شده:</b>\n"
    )

    buttons = [
        [InlineKeyboardButton(text="➕ افزودن لوکیشن جدید", callback_data="admin_add_loc_start")]
    ]

    if not servers:
        text += "ℹ️ <i>در حال حاضر هیچ لوکیشنی ثبت نشده است.</i>\n"
    else:
        for srv in servers:
            status_dot = "🟢 روشن" if srv.is_active else "🔴 خاموش"
            btn_text = f"{srv.flag_emoji} {srv.location_name} ({srv.location_code}) — {status_dot}"
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin_loc_detail:{srv.id}")])

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_loc_detail:"))
async def view_admin_location_detail(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """View details of a specific location/server and provide on/off toggle."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    server_id = uuid.UUID(callback.data.split(":")[1])
    server = await db_session.get(Server, server_id)
    if not server:
        return await callback.answer("لوکیشن یافت نشد.", show_alert=True)

    panel = await db_session.get(Panel, server.panel_id) if server.panel_id else None
    status_str = "🟢 روشن و فعال (موجود برای خرید مشتریان)" if server.is_active else "🔴 خاموش و غیرفعال (مخفی از لیست خرید)"
    toggle_text = "🔴 خاموش کردن این لوکیشن" if server.is_active else "🟢 روشن کردن این لوکیشن"

    text = (
        f"🌍 <b>مدیریت لوکیشن:</b> {server.flag_emoji} <b>{server.location_name}</b> (<code>{server.location_code}</code>)\n\n"
        f"🏷 نام سرور در سیستم: <b>{server.name}</b>\n"
        f"🖥 پنل 3x-ui متصل: <b>{panel.name if panel else 'نامشخص'}</b>\n"
        f"📡 هاست / دامنه اتصال: <code>{server.subscription_host}:{server.subscription_port}</code>\n"
        f"👥 ظرفیت سرور: <b>{server.current_clients} از {server.capacity_limit} کاربر</b>\n\n"
        f"وضعیت فعلی: <b>{status_str}</b>\n\n"
        f"جهت روشن/خاموش کردن، ویرایش یا حذف این لوکیشن، از گزینه‌های زیر استفاده نمایید:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"admin_toggle_loc:{server.id}")],
        [
            InlineKeyboardButton(text="✏️ ویرایش نام لوکیشن", callback_data=f"admin_edit_loc_name:{server.id}"),
            InlineKeyboardButton(text="📡 ویرایش دامنه/هاست", callback_data=f"admin_edit_loc_host:{server.id}"),
        ],
        [InlineKeyboardButton(text="🗑 حذف لوکیشن", callback_data=f"admin_delete_loc:{server.id}")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست لوکیشن‌ها", callback_data="admin_manage_locations")],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_toggle_loc:"))
async def toggle_location_status(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Toggle a server/location ON or OFF instantly."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    server_id = uuid.UUID(callback.data.split(":")[1])
    server = await db_session.get(Server, server_id)
    if not server:
        return await callback.answer("لوکیشن یافت نشد.", show_alert=True)

    server.is_active = not server.is_active
    if getattr(server, "inbound_id", None):
        inb = await db_session.get(Inbound, server.inbound_id)
        if inb:
            inb.is_active = server.is_active
    await db_session.commit()

    action_label = "روشن شد و در دسترس خرید کاربران قرار گرفت" if server.is_active else "خاموش شد و از منوی خرید مخفی گردید"
    await callback.answer(f"لوکیشن با موفقیت {action_label}.", show_alert=True)
    return await view_admin_location_detail(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_delete_loc:"))
async def prompt_delete_location(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    server_id = uuid.UUID(callback.data.split(":")[1])
    server = await db_session.get(Server, server_id)
    if not server:
        return await callback.answer("لوکیشن یافت نشد.", show_alert=True)
    text = (
        f"⚠️ <b>تایید حذف لوکیشن:</b>\n\n"
        f"آیا اطمینان دارید که می‌خواهید لوکیشن {server.flag_emoji} <b>{server.location_name}</b> را حذف کنید؟\n\n"
        f"<i>این لوکیشن به طور کامل از سیستم و منوی خرید حذف خواهد شد.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 بله، حذف شود", callback_data=f"admin_confirm_delete_loc:{server.id}"),
            InlineKeyboardButton(text="❌ انصراف", callback_data=f"admin_loc_detail:{server.id}"),
        ]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_confirm_delete_loc:"))
async def confirm_delete_location(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    server_id = uuid.UUID(callback.data.split(":")[1])
    server = await db_session.get(Server, server_id)
    if server:
        await db_session.delete(server)
        await db_session.commit()
    await callback.answer("لوکیشن با موفقیت حذف شد.", show_alert=True)
    return await list_admin_locations(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_edit_loc_name:"))
async def prompt_edit_loc_name(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    server_id = uuid.UUID(callback.data.split(":")[1])
    server = await db_session.get(Server, server_id)
    if not server:
        return await callback.answer("لوکیشن یافت نشد.", show_alert=True)
    await state.set_state(AdminState.editing_loc_name)
    await state.update_data(editing_server_id=str(server.id))
    text = (
        f"✏️ <b>ویرایش نام لوکیشن:</b>\n\n"
        f"نام فعلی: <b>{server.location_name}</b>\n\n"
        f"لطفاً <b>نام جدید لوکیشن</b> را وارد فرمایید (مثال: <code>آلمان (فرانکفورت)</code>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"admin_loc_detail:{server.id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_loc_name)
async def process_edit_loc_name(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        await state.clear()
        return
    data = await state.get_data()
    server_id_str = data.get("editing_server_id")
    server = await db_session.get(Server, uuid.UUID(server_id_str)) if server_id_str else None
    if not server:
        await state.clear()
        return await message.answer("⚠️ لوکیشن مورد نظر یافت نشد.")
    new_name = (message.text or "").strip()
    if len(new_name) < 2:
        return await message.answer("⚠️ نام لوکیشن باید حداقل ۲ کاراکتر باشد:")
    server.location_name = new_name
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>نام لوکیشن با موفقیت به «{new_name}» تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌍 مشاهده لوکیشن", callback_data=f"admin_loc_detail:{server.id}")],
            [InlineKeyboardButton(text="📋 لیست لوکیشن‌ها", callback_data="admin_manage_locations")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("admin_edit_loc_host:"))
async def prompt_edit_loc_host(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    server_id = uuid.UUID(callback.data.split(":")[1])
    server = await db_session.get(Server, server_id)
    if not server:
        return await callback.answer("لوکیشن یافت نشد.", show_alert=True)
    await state.set_state(AdminState.editing_loc_host)
    await state.update_data(editing_server_id=str(server.id))
    text = (
        f"📡 <b>ویرایش دامنه / هاست اتصال سابسکریپشن:</b>\n\n"
        f"هاست فعلی: <code>{server.subscription_host}</code>\n\n"
        f"لطفاً <b>دامنه یا IP جدید</b> را ارسال فرمایید (مثال: <code>de.subdomain.com</code> یا <code>185.120.40.10</code>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"admin_loc_detail:{server.id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_loc_host)
async def process_edit_loc_host(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        await state.clear()
        return
    data = await state.get_data()
    server_id_str = data.get("editing_server_id")
    server = await db_session.get(Server, uuid.UUID(server_id_str)) if server_id_str else None
    if not server:
        await state.clear()
        return await message.answer("⚠️ لوکیشن مورد نظر یافت نشد.")
    new_host = (message.text or "").strip().lower().replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
    if len(new_host) < 3:
        return await message.answer("⚠️ لطفاً یک هاست یا IP معتبر ارسال فرمایید:")
    server.subscription_host = new_host
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ <b>هاست سابسکریپشن با موفقیت به <code>{new_host}</code> تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌍 مشاهده لوکیشن", callback_data=f"admin_loc_detail:{server.id}")],
            [InlineKeyboardButton(text="📋 لیست لوکیشن‌ها", callback_data="admin_manage_locations")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_add_loc_start")
async def start_add_location(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    """Step 1 of wizard: Prompt for Location Name."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    panel_res = await db_session.execute(select(Panel).limit(1))
    panel = panel_res.scalar_one_or_none()
    if not panel:
        return await callback.answer("⚠️ ابتدا باید حداقل یک پنل 3x-ui در بخش «سرورها و پنل‌ها» ثبت نمایید.", show_alert=True)

    await state.set_state(AdminState.loc_entering_name)
    text = (
        "➕ <b>افزودن لوکیشن جدید — مرحله ۱ از ۴:</b>\n\n"
        "لطفاً <b>نام لوکیشن / شهر / کشور</b> را به فارسی ارسال فرمایید:\n\n"
        "💡 مثال: <code>فرانسه (پاریس)</code> یا <code>انگلیس (لندن)</code> یا <code>آمریکا</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_locations")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.loc_entering_name)
async def process_loc_name(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    name = (message.text or "").strip()
    if len(name) < 2:
        return await message.answer("⚠️ نام لوکیشن باید حداقل ۲ کاراکتر باشد:")
    await state.update_data(new_loc_name=name)
    await state.set_state(AdminState.loc_entering_code)
    await message.answer(
        f"🌐 <b>مرحله ۲ از ۴ (کد کشور):</b>\n\n"
        f"لوکیشن: <b>{name}</b>\n\n"
        f"لطفاً <b>کد دو حرفی کشور</b> را به حروف لاتین وارد فرمایید (مثال: <code>FR</code> یا <code>UK</code> یا <code>US</code> یا <code>DE</code>):",
        parse_mode="HTML"
    )


@router.message(AdminState.loc_entering_code)
async def process_loc_code(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    code = (message.text or "").strip().upper()
    if len(code) < 2 or len(code) > 8:
        return await message.answer("⚠️ لطفاً یک کد کوتاه معتبر (مثال: FR یا US) وارد کنید:")
    await state.update_data(new_loc_code=code)
    await state.set_state(AdminState.loc_entering_flag)
    await message.answer(
        f"🚩 <b>مرحله ۳ از ۴ (پرچم کشور):</b>\n\n"
        f"لطفاً <b>ایموجی پرچم کشور</b> را ارسال نمایید (مثال: <code>🇫🇷</code> یا <code>🇬🇧</code> یا <code>🇺🇸</code> یا <code>🌐</code>):",
        parse_mode="HTML"
    )


@router.message(AdminState.loc_entering_flag)
async def process_loc_flag(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    flag = (message.text or "").strip() or "🌐"
    await state.update_data(new_loc_flag=flag)
    await state.set_state(AdminState.loc_entering_host)
    await message.answer(
        f"📡 <b>مرحله ۴ از ۴ (هاست یا دامنه سرور):</b>\n\n"
        f"لطفاً <b>دامنه یا IP سرور</b> را برای ایجاد لینک سابسکریپشن وارد فرمایید:\n"
        f"💡 مثال: <code>fr.servervpn.com</code> یا <code>185.120.40.10</code>",
        parse_mode="HTML"
    )


@router.message(AdminState.loc_entering_host)
async def process_loc_host(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return
    host = (message.text or "").strip().lower().replace("http://", "").replace("https://", "").split("/")[0].split(":")[0]
    if len(host) < 3:
        return await message.answer("⚠️ لطفاً یک دامنه یا IP معتبر وارد فرمایید:")

    data = await state.get_data()
    loc_name = data.get("new_loc_name", "لوکیشن جدید")
    loc_code = data.get("new_loc_code", "XX")
    flag = data.get("new_loc_flag", "🌐")

    panel_res = await db_session.execute(select(Panel).limit(1))
    panel = panel_res.scalar_one_or_none()

    server_name = f"سرور {loc_name} ({loc_code})"
    new_server = Server(
        id=uuid.uuid4(),
        name=server_name,
        location_code=loc_code,
        location_name=loc_name,
        flag_emoji=flag,
        panel_id=panel.id if panel else None,
        subscription_host=host,
        subscription_port=2096,
        is_active=True,
        priority=10,
    )
    db_session.add(new_server)
    await db_session.commit()
    await state.clear()

    text = (
        f"✅ <b>لوکیشن جدید با موفقیت اضافه شد و روشن گردید!</b>\n\n"
        f"• لوکیشن: {flag} <b>{loc_name}</b> (<code>{loc_code}</code>)\n"
        f"• هاست: <code>{host}</code>\n"
        f"• وضعیت: <b>🟢 روشن (آماده خرید توسط مشتریان)</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 مشاهده لوکیشن", callback_data=f"admin_loc_detail:{new_server.id}")],
        [InlineKeyboardButton(text="📋 لیست تمام لوکیشن‌ها", callback_data="admin_manage_locations")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ==============================================================================
# ADVANCED BOT FEATURES: BROADCAST & BOT SETTINGS
# ==============================================================================

@router.callback_query(F.data == "admin_prompt_broadcast")
async def prompt_broadcast(callback: CallbackQuery, state: FSMContext, db_user: User):
    """Segmented broadcast message feature."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    text = (
        "📢 <b>ارسال پیام همگانی هوشمند (Segmented Broadcast):</b>\n\n"
        "لطفاً گروه هدف مورد نظر خود را جهت ارسال پیام انتخاب نمایید:\n\n"
        "👥 <b>همه کاربران:</b> ارسال پیام به کلیه کاربران ربات\n"
        "🟢 <b>دارای اشتراک فعال:</b> کاربرانی که اشتراک معتبر و فعال دارند\n"
        "🔴 <b>دارای اشتراک منقضی:</b> کاربرانی که سرویس‌شان به پایان رسیده\n"
        "⚪️ <b>بدون سابقه خرید:</b> کاربرانی که در ربات عضو هستند ولی تا بحال خریدی نکرده‌اند"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 همه کاربران", callback_data="admin_bcast_seg:all"),
            InlineKeyboardButton(text="🟢 دارای اشتراک فعال", callback_data="admin_bcast_seg:active"),
        ],
        [
            InlineKeyboardButton(text="🔴 دارای اشتراک منقضی", callback_data="admin_bcast_seg:expired"),
            InlineKeyboardButton(text="⚪️ بدون سابقه خرید", callback_data="admin_bcast_seg:nobuy"),
        ],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_refresh_dashboard")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_bcast_seg:"))
async def select_broadcast_segment(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    segment = callback.data.split(":")[1]
    await state.set_state(AdminState.entering_broadcast_message)
    await state.update_data(broadcast_segment=segment)

    seg_names = {
        "all": "👥 همه کاربران",
        "active": "🟢 کاربران دارای اشتراک فعال",
        "expired": "🔴 کاربران دارای اشتراک منقضی",
        "nobuy": "⚪️ کاربران بدون سابقه خرید",
    }
    text = (
        f"📢 گروه هدف انتخاب شد: <b>{seg_names.get(segment, segment)}</b>\n\n"
        "لطفاً <b>متن اطلاعیه یا پیام</b> خود را ارسال فرمایید (پشتیبانی از عکس و متن و تگ‌های HTML):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_refresh_dashboard")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.entering_broadcast_message)
async def process_broadcast_message(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        await state.clear()
        return

    text_to_send = message.text or message.caption
    if not text_to_send:
        return await message.answer("⚠️ لطفاً پیامی حاوی متن ارسال فرمایید.")

    data = await state.get_data()
    segment = data.get("broadcast_segment", "all")
    await state.clear()

    status_msg = await message.answer("⏳ در حال استخراج کاربران و ارسال پیام...")

    from datetime import datetime, timezone
    from app.database.models.subscription import Subscription
    from app.database.models.order import Order
    now = datetime.now(timezone.utc)

    if segment == "active":
        res = await db_session.execute(
            select(User.telegram_id).join(Subscription, Subscription.user_id == User.id)
            .where(User.is_banned.is_(False), Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at > now)
            .distinct()
        )
    elif segment == "expired":
        res = await db_session.execute(
            select(User.telegram_id).join(Subscription, Subscription.user_id == User.id)
            .where(User.is_banned.is_(False), (Subscription.status != SubscriptionStatus.ACTIVE) | (Subscription.expires_at <= now))
            .distinct()
        )
    elif segment == "nobuy":
        subquery_orders = select(Order.user_id).distinct()
        res = await db_session.execute(
            select(User.telegram_id).where(User.is_banned.is_(False), User.id.not_in(subquery_orders))
        )
    else:
        res = await db_session.execute(
            select(User.telegram_id).where(User.is_banned.is_(False))
        )

    tg_ids = [r[0] for r in res.fetchall() if r[0]]

    success_count = 0
    fail_count = 0
    import asyncio
    for tid in tg_ids:
        try:
            if message.photo:
                try:
                    await message.bot.send_photo(
                        chat_id=tid,
                        photo=message.photo[-1].file_id,
                        caption=f"📢 <b>اطلاعیه مدیریت:</b>\n\n{text_to_send}",
                        parse_mode="HTML"
                    )
                except Exception:
                    await message.bot.send_photo(
                        chat_id=tid,
                        photo=message.photo[-1].file_id,
                        caption=f"📢 <b>اطلاعیه مدیریت:</b>\n\n{html.escape(text_to_send)}",
                        parse_mode="HTML"
                    )
            else:
                try:
                    await message.bot.send_message(
                        chat_id=tid,
                        text=f"📢 <b>اطلاعیه مدیریت:</b>\n\n{text_to_send}",
                        parse_mode="HTML"
                    )
                except Exception:
                    await message.bot.send_message(
                        chat_id=tid,
                        text=f"📢 <b>اطلاعیه مدیریت:</b>\n\n{html.escape(text_to_send)}",
                        parse_mode="HTML"
                    )
            success_count += 1
            await asyncio.sleep(0.04)
        except Exception:
            fail_count += 1

    report_text = (
        f"📢 <b>گزارش ارسال پیام همگانی هوشمند:</b>\n\n"
        f"🎯 گروه هدف: <b>{segment}</b>\n"
        f"✅ ارسال موفق: <b>{success_count} کاربر</b>\n"
        f"❌ ناموفق یا بلاک ربات: <b>{fail_count}</b>\n\n"
        f"عملیات برودکست با موفقیت به پایان رسید."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")]
    ])
    await status_msg.edit_text(report_text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_bot_settings")
async def show_bot_settings(callback: CallbackQuery, db_user: User):
    """Full bot controls and system settings."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    channel_status = "🟢 روشن (اجباری)" if getattr(settings, "MANDATORY_CHANNEL_ENABLED", False) else "🔴 خاموش (غیرفعال)"
    trial_status = "🟢 روشن (فعال)" if getattr(settings, "TRIAL_ENABLED", False) else "🔴 خاموش (غیرفعال)"
    c2c_status = "🟢 روشن (فعال)" if getattr(settings, "CARD_TO_CARD_ENABLED", True) else "🔴 خاموش (غیرفعال)"

    text = (
        "⚙️ <b>تنظیمات عمومی ربات و قابلیت‌های سیستم:</b>\n\n"
        f"📢 <b>قفل عضویت اجباری کانال:</b> <b>{channel_status}</b>\n"
        f"• کانال تنظیم شده: <code>@{settings.CHANNEL_USERNAME}</code>\n\n"
        f"🎁 <b>اکانت تست رایگان برای کاربران:</b> <b>{trial_status}</b>\n"
        f"• مشخصات تست: {settings.TRIAL_TRAFFIC_MB} مگابایت — {settings.TRIAL_DURATION_HOURS} ساعت\n\n"
        f"💳 <b>درگاه کارت به کارت:</b> <b>{c2c_status}</b>\n"
        f"• شماره کارت: <code>{settings.CARD_NUMBER}</code>\n"
        f"• دارنده: <b>{settings.CARD_HOLDER}</b>\n"
        f"• بانک: <b>{settings.CARD_BANK_NAME}</b>\n\n"
        "💡 جهت تغییر وضعیت هر قابلیت، روی کلید مربوطه کلیک فرمایید:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📢 تغییر قفل کانال", callback_data="admin_toggle_channel_lock"),
            InlineKeyboardButton(text="✏️ تغییر آیدی کانال", callback_data="admin_edit_channel_username"),
        ],
        [
            InlineKeyboardButton(text="🎁 مدیریت کامل اکانت تست", callback_data="admin_manage_trial"),
            InlineKeyboardButton(text="💳 مدیریت روش‌های پرداخت", callback_data="admin_manage_payments"),
        ],
        [
            InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard"),
        ],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


from app.core.config import update_env_variable


def sync_admin_ids_to_env():
    """Sync settings.ADMIN_TELEGRAM_IDS back into .env file."""
    ids_str = ",".join(str(x) for x in settings.ADMIN_TELEGRAM_IDS)
    update_env_variable("ADMIN_TELEGRAM_IDS", ids_str)


# ==========================================
# ADVANCED PAYMENT METHODS MANAGEMENT (CARD / CRYPTO / ZARINPAL / MIN TOPUP)
# ==========================================

@router.callback_query(F.data == "admin_manage_payments")
async def show_payment_methods_settings(callback: CallbackQuery, db_user: User):
    """Full payment methods and gateways management center."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    c2c_st = "🟢 فعال" if getattr(settings, "CARD_TO_CARD_ENABLED", True) else "🔴 غیرفعال"
    crypto_st = "🟢 فعال" if getattr(settings, "CRYPTO_ENABLED", True) else "🔴 غیرفعال"
    zarinpal_st = "🟢 فعال" if getattr(settings, "ZARINPAL_ENABLED", False) else "🔴 غیرفعال"

    text = (
        "💳 <b>مدیریت جامع روش‌ها و درگاه‌های پرداخت:</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>۱. روش کارت به کارت:</b> <b>{c2c_st}</b>\n"
        f"• شماره کارت: <code>{settings.CARD_NUMBER}</code>\n"
        f"• نام صاحب حساب: <b>{settings.CARD_HOLDER}</b>\n"
        f"• نام بانک: <b>{settings.CARD_BANK_NAME}</b>\n"
        f"• شماره شبا: <code>{settings.CARD_IBAN}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>۲. روش ارز دیجیتال (تتر USDT - شبکه TRC20):</b> <b>{crypto_st}</b>\n"
        f"• آدرس ولت تتر: <code>{settings.CRYPTO_WALLET_TRC20}</code>\n"
        f"• نرخ هر تتر: <b>{getattr(settings, 'USDT_TOMAN_RATE', 70000):,} تومان</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>۳. درگاه پرداخت آنلاین ریالی (زرین‌پال):</b> <b>{zarinpal_st}</b>\n"
        f"• مرچنت کد: <code>{settings.ZARINPAL_MERCHANT_ID}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>حداقل مبلغ شارژ کیف پول:</b> <b>{settings.MIN_WALLET_TOPUP_TOMAN:,} تومان</b>\n\n"
        "👇 <i>جهت فعال/غیرفعال‌سازی یا ویرایش اطلاعات هر بخش، دکمه مربوطه را انتخاب نمایید:</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 قطع/وصل کارت‌به‌کارت", callback_data="admin_toggle_c2c_direct"),
            InlineKeyboardButton(text="🪙 قطع/وصل کریپتو (تتر)", callback_data="admin_toggle_crypto_direct"),
        ],
        [
            InlineKeyboardButton(text="✏️ ویرایش شماره کارت", callback_data="admin_edit_card_num"),
            InlineKeyboardButton(text="✏️ ویرایش نام صاحب حساب", callback_data="admin_edit_card_holder"),
        ],
        [
            InlineKeyboardButton(text="✏️ ویرایش نام بانک", callback_data="admin_edit_card_bank"),
            InlineKeyboardButton(text="✏️ ویرایش ولت تتر (TRC20)", callback_data="admin_edit_crypto_wallet"),
        ],
        [
            InlineKeyboardButton(text="💵 تغییر نرخ تتر (تومان)", callback_data="admin_edit_usdt_rate"),
            InlineKeyboardButton(text="🌐 قطع/وصل درگاه زرین‌پال", callback_data="admin_toggle_zarinpal_direct"),
        ],
        [
            InlineKeyboardButton(text="🔑 ویرایش مرچنت زرین‌پال", callback_data="admin_edit_zarinpal_merchant"),
            InlineKeyboardButton(text="💰 حداقل شارژ کیف پول", callback_data="admin_edit_min_wallet"),
        ],
        [
            InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard"),
        ],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_toggle_c2c_direct")
async def toggle_c2c_direct(callback: CallbackQuery, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    settings.CARD_TO_CARD_ENABLED = not getattr(settings, "CARD_TO_CARD_ENABLED", True)
    update_env_variable("CARD_TO_CARD_ENABLED", str(settings.CARD_TO_CARD_ENABLED).lower())
    st = "فعال شد" if settings.CARD_TO_CARD_ENABLED else "غیرفعال شد"
    await callback.answer(f"کارت به کارت {st}.", show_alert=True)
    return await show_payment_methods_settings(callback, db_user)


@router.callback_query(F.data == "admin_toggle_crypto_direct")
async def toggle_crypto_direct(callback: CallbackQuery, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    settings.CRYPTO_ENABLED = not getattr(settings, "CRYPTO_ENABLED", True)
    update_env_variable("CRYPTO_ENABLED", str(settings.CRYPTO_ENABLED).lower())
    st = "فعال شد" if settings.CRYPTO_ENABLED else "غیرفعال شد"
    await callback.answer(f"پرداخت ارز دیجیتال (تتر) {st}.", show_alert=True)
    return await show_payment_methods_settings(callback, db_user)


@router.callback_query(F.data == "admin_toggle_zarinpal_direct")
async def toggle_zarinpal_direct(callback: CallbackQuery, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    settings.ZARINPAL_ENABLED = not getattr(settings, "ZARINPAL_ENABLED", False)
    update_env_variable("ZARINPAL_ENABLED", str(settings.ZARINPAL_ENABLED).lower())
    st = "فعال شد" if settings.ZARINPAL_ENABLED else "غیرفعال شد"
    await callback.answer(f"درگاه آنلاین زرین‌پال {st}.", show_alert=True)
    return await show_payment_methods_settings(callback, db_user)


@router.callback_query(F.data == "admin_toggle_channel_lock")
async def toggle_channel_lock(callback: CallbackQuery, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    settings.MANDATORY_CHANNEL_ENABLED = not getattr(settings, "MANDATORY_CHANNEL_ENABLED", False)
    update_env_variable("MANDATORY_CHANNEL_ENABLED", str(settings.MANDATORY_CHANNEL_ENABLED).lower())
    status_label = "فعال شد (کاربران باید قبل از استفاده عضو کانال شوند)" if settings.MANDATORY_CHANNEL_ENABLED else "غیرفعال شد"
    await callback.answer(f"قفل جوین اجباری کانال {status_label}.", show_alert=True)
    return await show_bot_settings(callback, db_user)


@router.callback_query(F.data == "admin_edit_channel_username")
async def start_edit_channel_username(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.editing_channel_username)
    await callback.message.edit_text(
        "📢 <b>ویرایش آیدی کانال رسمی ربات:</b>\n\n"
        f"آیدی فعلی: <code>@{settings.CHANNEL_USERNAME}</code>\n\n"
        "لطفاً آیدی جدید کانال تلگرام را (با یا بدون @) ارسال فرمایید:\n"
        "<i>(نکته مهم: برای بررسی عضویت کاربران، حتماً باید ربات در کانال شما ادمین باشد)</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_bot_settings")]
        ]),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminState.editing_channel_username)
async def process_edit_channel_username(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    new_username = message.text.strip().lstrip("@")
    if not new_username:
        return await message.answer("⚠️ آیدی کانال نمی‌تواند خالی باشد. لطفاً مجدداً ارسال فرمایید.")

    settings.CHANNEL_USERNAME = new_username
    update_env_variable("CHANNEL_USERNAME", new_username)
    await state.clear()
    await message.answer(
        f"✅ <b>آیدی کانال با موفقیت به <code>@{new_username}</code> تغییر یافت.</b>\n"
        f"<i>اطمینان حاصل فرمایید که ربات در این کانال با دسترسی Administrator عضو شده باشد.</i>",
        parse_mode="HTML",
    )


# ==========================================
# FREE TRIAL COMPREHENSIVE MANAGEMENT (LIMITS, TRAFFIC, DURATION, INBOUND)
# ==========================================

@router.callback_query(F.data == "admin_manage_trial")
async def show_admin_trial_management(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Full management panel for Free Trial accounts (limits, traffic, duration, inbound)."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    trial_status = "🟢 روشن (فعال)" if getattr(settings, "TRIAL_ENABLED", True) else "🔴 خاموش (غیرفعال)"
    traffic_gb = getattr(settings, "TRIAL_TRAFFIC_MB", 1024) / 1024
    traffic_str = f"{traffic_gb:g} گیگابایت"
    duration_days = getattr(settings, "TRIAL_DURATION_DAYS", 1)
    max_count = getattr(settings, "TRIAL_MAX_COUNT", 1)

    inbound_desc = "⚡️ خودکار (اولین اینباند فعال)"
    selected_inbound_id = getattr(settings, "TRIAL_INBOUND_ID", None)
    if selected_inbound_id:
        try:
            inb = await db_session.get(Inbound, uuid.UUID(selected_inbound_id))
            if inb:
                inbound_desc = f"📡 {inb.remark or inb.protocol} (پورت {inb.port} - {inb.protocol.upper()})"
        except Exception:
            pass

    text = (
        "🎁 <b>پنل تنظیمات و مدیریت اکانت تست رایگان:</b>\n\n"
        f"• وضعیت ارائه تست: <b>{trial_status}</b>\n"
        f"• سقف دریافت هر کاربر: <b>{max_count} مرتبه</b>\n"
        f"• حجم ترافیک اکانت تست: <b>{traffic_str}</b> ({settings.TRIAL_TRAFFIC_MB} MB)\n"
        f"• مدت اعتبار اکانت تست: <b>{duration_days} روز</b> ({settings.TRIAL_DURATION_HOURS} ساعت)\n"
        f"• اینباند ساخت اکانت تست: <b>{inbound_desc}</b>\n\n"
        "👇 جهت تغییر هر یک از مشخصات فوق روی کلید مربوطه کلیک نمایید:"
    )

    toggle_btn_text = "🔴 خاموش کردن تست" if settings.TRIAL_ENABLED else "🟢 روشن کردن تست"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"🔄 {toggle_btn_text}", callback_data="admin_trial_toggle"),
        ],
        [
            InlineKeyboardButton(text="🔢 سقف دفعات دریافت", callback_data="admin_trial_prompt_max_count"),
            InlineKeyboardButton(text="📊 تغییر حجم (GB)", callback_data="admin_trial_prompt_traffic"),
        ],
        [
            InlineKeyboardButton(text="⏳ تغییر مدت زمان (روز)", callback_data="admin_trial_prompt_duration"),
            InlineKeyboardButton(text="📡 انتخاب اینباند تست", callback_data="admin_trial_select_inbound"),
        ],
        [
            InlineKeyboardButton(text="⚙️ تنظیمات عمومی", callback_data="admin_bot_settings"),
            InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard"),
        ],
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_(["admin_trial_toggle", "admin_toggle_trial"]))
async def toggle_trial_status(callback: CallbackQuery, db_user: User, db_session: AsyncSession = None):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    settings.TRIAL_ENABLED = not getattr(settings, "TRIAL_ENABLED", True)
    update_env_variable("TRIAL_ENABLED", str(settings.TRIAL_ENABLED).lower())
    status_label = "روشن شد (کاربران می‌توانند اکانت تست دریافت کنند)" if settings.TRIAL_ENABLED else "خاموش شد"
    await callback.answer(f"✅ اکانت تست {status_label}", show_alert=True)
    if db_session:
        await show_admin_trial_management(callback, db_user, db_session)
    else:
        await show_bot_settings(callback, db_user)


@router.callback_query(F.data == "admin_trial_prompt_max_count")
async def prompt_trial_max_count(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.setting_trial_max_count)
    text = (
        f"🔢 <b>تنظیم سقف دفعات دریافت اکانت تست:</b>\n\n"
        f"سقف فعلی: <b>{getattr(settings, 'TRIAL_MAX_COUNT', 1)} بار</b> برای هر کاربر\n\n"
        f"لطفاً تعداد مجاز جدید را به صورت عدد انگلیسی ارسال فرمایید (مثلاً <code>1</code> یا <code>2</code> یا <code>5</code>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_trial")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.setting_trial_max_count)
async def process_trial_max_count(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    txt = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not txt.isdigit() or int(txt) < 1:
        return await message.answer("⚠️ لطفاً یک عدد صحیح معتبر بزرگتر از صفر وارد نمایید (مثلاً ۱ یا ۲):")

    val = int(txt)
    settings.TRIAL_MAX_COUNT = val
    update_env_variable("TRIAL_MAX_COUNT", str(val))
    await state.clear()
    await message.answer(
        f"✅ <b>سقف دفعات دریافت اکانت تست با موفقیت روی {val} بار برای هر کاربر تنظیم شد.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 بازگشت به مدیریت اکانت تست", callback_data="admin_manage_trial")],
            [InlineKeyboardButton(text="⚙️ بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_trial_prompt_traffic")
async def prompt_trial_traffic(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.setting_trial_traffic_gb)
    cur_gb = getattr(settings, "TRIAL_TRAFFIC_MB", 1024) / 1024
    text = (
        f"📊 <b>تنظیم حجم ترافیک اکانت تست:</b>\n\n"
        f"حجم فعلی: <b>{cur_gb:g} گیگابایت</b> ({settings.TRIAL_TRAFFIC_MB} MB)\n\n"
        f"لطفاً حجم جدید را بر حسب <b>گیگابایت (GB)</b> ارسال فرمایید (مثلاً <code>1</code> یا <code>2</code> یا <code>0.5</code>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_trial")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.setting_trial_traffic_gb)
async def process_trial_traffic(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    txt = (message.text or "").strip().replace("/", ".").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    try:
        gb = float(txt)
        if gb <= 0:
            raise ValueError()
    except ValueError:
        return await message.answer("⚠️ لطفاً یک مقدار عددی معتبر (مثلاً ۱ یا ۲ یا ۰.۵) وارد فرمایید:")

    mb = int(gb * 1024)
    settings.TRIAL_TRAFFIC_MB = mb
    update_env_variable("TRIAL_TRAFFIC_MB", str(mb))
    await state.clear()
    await message.answer(
        f"✅ <b>حجم اکانت تست با موفقیت روی {gb:g} گیگابایت ({mb} مگابایت) تنظیم شد.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 بازگشت به مدیریت اکانت تست", callback_data="admin_manage_trial")],
            [InlineKeyboardButton(text="⚙️ بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_trial_prompt_duration")
async def prompt_trial_duration(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.setting_trial_duration_days)
    cur_days = getattr(settings, "TRIAL_DURATION_DAYS", 1)
    text = (
        f"⏳ <b>تنظیم مدت زمان اعتبار اکانت تست:</b>\n\n"
        f"مدت فعلی: <b>{cur_days} روز</b> ({settings.TRIAL_DURATION_HOURS} ساعت)\n\n"
        f"لطفاً تعداد روزهای اعتبار جدید را به صورت عدد وارد فرمایید (مثلاً <code>1</code> یا <code>3</code> یا <code>7</code>):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_trial")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.setting_trial_duration_days)
async def process_trial_duration(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    txt = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    if not txt.isdigit() or int(txt) < 1:
        return await message.answer("⚠️ لطفاً یک عدد صحیح معتبر بر حسب روز (مثلاً ۱ یا ۳) وارد فرمایید:")

    days = int(txt)
    settings.TRIAL_DURATION_DAYS = days
    settings.TRIAL_DURATION_HOURS = days * 24
    update_env_variable("TRIAL_DURATION_DAYS", str(days))
    update_env_variable("TRIAL_DURATION_HOURS", str(days * 24))
    await state.clear()
    await message.answer(
        f"✅ <b>مدت اعتبار اکانت تست با موفقیت روی {days} روز ({days * 24} ساعت) تنظیم شد.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 بازگشت به مدیریت اکانت تست", callback_data="admin_manage_trial")],
            [InlineKeyboardButton(text="⚙️ بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_trial_select_inbound")
async def select_trial_inbound_menu(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Show list of inbounds so admin can pick which inbound receives trial users."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    inbounds_res = await db_session.execute(
        select(Inbound).where(Inbound.is_active == True).order_by(Inbound.panel_id, Inbound.port)
    )
    inbounds = list(inbounds_res.scalars().all())

    buttons = [
        [
            InlineKeyboardButton(text="⚡️ انتخاب خودکار (اولین اینباند فعال)", callback_data="admin_trial_set_inb:auto")
        ]
    ]

    cur_selected = getattr(settings, "TRIAL_INBOUND_ID", None)

    for inb in inbounds:
        tag = inb.remark or inb.protocol
        is_cur = " (فعلی ✅)" if str(inb.id) == str(cur_selected) else ""
        btn_text = f"📡 {tag} [{inb.protocol.upper()}:{inb.port}]{is_cur}"
        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"admin_trial_set_inb:{inb.id}")
        ])

    buttons.append([
        InlineKeyboardButton(text="🔙 بازگشت به تنظیمات تست", callback_data="admin_manage_trial")
    ])

    text = (
        "📡 <b>انتخاب اینباند اختصاصی برای ایجاد اکانت‌های تست:</b>\n\n"
        "شما می‌توانید مشخص کنید کلاینت‌های تست روی کدام اینباند از پنل متصل ساخته شوند، یا اجازه دهید سیستم به صورت خودکار اولین اینباند را انتخاب کند:\n\n"
        "👇 لطفاً اینباند مورد نظر را انتخاب فرمایید:"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_trial_set_inb:"))
async def process_trial_set_inbound(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    inb_val = callback.data.split(":")[1]
    if inb_val == "auto":
        settings.TRIAL_INBOUND_ID = None
        update_env_variable("TRIAL_INBOUND_ID", "")
        await callback.answer("✅ اینباند تست روی حالت خودکار تنظیم شد.", show_alert=True)
    else:
        inb = await db_session.get(Inbound, uuid.UUID(inb_val))
        if not inb:
            return await callback.answer("اینباند یافت نشد.", show_alert=True)
        settings.TRIAL_INBOUND_ID = str(inb.id)
        update_env_variable("TRIAL_INBOUND_ID", str(inb.id))
        tag = inb.remark or inb.protocol
        await callback.answer(f"✅ اینباند اکانت تست روی {tag} (پورت {inb.port}) تنظیم شد.", show_alert=True)

    await show_admin_trial_management(callback, db_user, db_session)


@router.callback_query(F.data == "admin_toggle_c2c")
async def toggle_c2c_status(callback: CallbackQuery, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    settings.CARD_TO_CARD_ENABLED = not getattr(settings, "CARD_TO_CARD_ENABLED", True)
    update_env_variable("CARD_TO_CARD_ENABLED", str(settings.CARD_TO_CARD_ENABLED).lower())
    status_label = "فعال شد" if settings.CARD_TO_CARD_ENABLED else "غیرفعال شد"
    await callback.answer(f"پرداخت کارت به کارت {status_label}.", show_alert=True)
    return await show_bot_settings(callback, db_user)


@router.callback_query(F.data == "admin_edit_card_num")
async def prompt_edit_card_num(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.editing_card_number)
    text = (
        "💳 <b>ویرایش شماره کارت بانکی:</b>\n\n"
        f"شماره کارت فعلی: <code>{settings.CARD_NUMBER}</code>\n\n"
        "لطفاً <b>شماره ۱۶ رقمی کارت بانکی جدید</b> را ارسال فرمایید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_payments")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_card_number)
async def process_edit_card_num(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    digits = (message.text or "").strip().replace("-", "").replace(" ", "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
    if not digits.isdigit() or len(digits) != 16:
        return await message.answer("⚠️ لطفاً یک شماره کارت ۱۶ رقمی معتبر وارد فرمایید:")
    settings.CARD_NUMBER = digits
    update_env_variable("CARD_NUMBER", digits)
    await state.clear()
    await message.answer(
        f"✅ <b>شماره کارت با موفقیت به <code>{digits}</code> تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 مدیریت روش‌های پرداخت", callback_data="admin_manage_payments")],
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_edit_card_holder")
async def prompt_edit_card_holder(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.editing_card_holder)
    text = (
        "💳 <b>ویرایش نام صاحب حساب کارت بانکی:</b>\n\n"
        f"نام فعلی: <b>{settings.CARD_HOLDER}</b>\n\n"
        "لطفاً <b>نام و نام خانوادگی صاحب حساب جدید</b> را ارسال فرمایید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_payments")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_card_holder)
async def process_edit_card_holder(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    name = (message.text or "").strip()
    if len(name) < 3:
        return await message.answer("⚠️ لطفاً نام معتبری وارد فرمایید:")
    settings.CARD_HOLDER = name
    update_env_variable("CARD_HOLDER", name)
    await state.clear()
    await message.answer(
        f"✅ <b>نام صاحب حساب با موفقیت به «{name}» تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 مدیریت روش‌های پرداخت", callback_data="admin_manage_payments")],
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_edit_card_bank")
async def prompt_edit_card_bank(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.editing_card_bank)
    text = (
        "🏦 <b>ویرایش نام بانک:</b>\n\n"
        f"بانک فعلی: <b>{settings.CARD_BANK_NAME}</b>\n\n"
        "لطفاً <b>نام بانک صادرکننده کارت</b> را ارسال فرمایید (مثال: بانک سامان، بانک ملی):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_payments")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_card_bank)
async def process_edit_card_bank(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    bname = (message.text or "").strip()
    if len(bname) < 2:
        return await message.answer("⚠️ لطفاً نام بانک معتبری وارد فرمایید:")
    settings.CARD_BANK_NAME = bname
    update_env_variable("CARD_BANK_NAME", bname)
    await state.clear()
    await message.answer(
        f"✅ <b>نام بانک با موفقیت به «{bname}» تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 مدیریت روش‌های پرداخت", callback_data="admin_manage_payments")],
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_edit_crypto_wallet")
async def prompt_edit_crypto_wallet(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.editing_crypto_wallet)
    text = (
        "🪙 <b>ویرایش آدرس کیف پول تتر (USDT - TRC20):</b>\n\n"
        f"آدرس فعلی: <code>{settings.CRYPTO_WALLET_TRC20}</code>\n\n"
        "لطفاً <b>آدرس کیف پول تتر شبکه TRC-20 (Tron) جدید</b> را ارسال فرمایید:\n"
        "<i>(آدرس ولت ترون با حرف T شروع می‌شود)</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_payments")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_crypto_wallet)
async def process_edit_crypto_wallet(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    addr = (message.text or "").strip()
    if not addr.startswith("T") or len(addr) < 26:
        return await message.answer("⚠️ آدرس کیف پول ترون نامعتبر است. آدرس باید با حرف T انگلیسی شروع شود و حداقل ۲۶ کاراکتر باشد:")
    settings.CRYPTO_WALLET_TRC20 = addr
    update_env_variable("CRYPTO_WALLET_TRC20", addr)
    await state.clear()
    await message.answer(
        f"✅ <b>آدرس کیف پول تتر TRC20 با موفقیت ذخیره شد:</b>\n<code>{addr}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 مدیریت روش‌های پرداخت", callback_data="admin_manage_payments")],
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_edit_usdt_rate")
async def prompt_edit_usdt_rate(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.editing_usdt_rate)
    text = (
        "💵 <b>ویرایش نرخ تبدیل تتر به تومان:</b>\n\n"
        f"نرخ فعلی: <b>{getattr(settings, 'USDT_TOMAN_RATE', 70000):,} تومان</b>\n\n"
        "لطفاً <b>نرخ هر ۱ تتر به تومان</b> را به صورت عدد ارسال فرمایید:\n"
        "(مثال: برای ۶۸ هزار تومان عدد <code>68000</code> را بفرستید)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_payments")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_usdt_rate)
async def process_edit_usdt_rate(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    digits = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
    if not digits.isdigit() or int(digits) < 1000:
        return await message.answer("⚠️ لطفاً یک رقم معتبر بر حسب تومان وارد فرمایید (مثال: 70000):")
    rate = int(digits)
    settings.USDT_TOMAN_RATE = rate
    update_env_variable("USDT_TOMAN_RATE", str(rate))
    await state.clear()
    await message.answer(
        f"✅ <b>نرخ هر تتر با موفقیت روی {rate:,} تومان تنظیم گردید.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 مدیریت روش‌های پرداخت", callback_data="admin_manage_payments")],
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_edit_zarinpal_merchant")
async def prompt_edit_zarinpal_merchant(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.editing_zarinpal_merchant)
    text = (
        "🔑 <b>ویرایش مرچنت کد زرین‌پال (Merchant ID):</b>\n\n"
        f"مرچنت کد فعلی: <code>{settings.ZARINPAL_MERCHANT_ID}</code>\n\n"
        "لطفاً <b>مرچنت کد ۳۶ کاراکتری زرین‌پال</b> خود را ارسال فرمایید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_payments")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_zarinpal_merchant)
async def process_edit_zarinpal_merchant(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    merchant = (message.text or "").strip()
    if len(merchant) < 10:
        return await message.answer("⚠️ لطفاً مرچنت کد معتبری وارد فرمایید:")
    settings.ZARINPAL_MERCHANT_ID = merchant
    update_env_variable("ZARINPAL_MERCHANT_ID", merchant)
    await state.clear()
    await message.answer(
        f"✅ <b>مرچنت کد زرین‌پال با موفقیت ذخیره شد:</b>\n<code>{merchant}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 مدیریت روش‌های پرداخت", callback_data="admin_manage_payments")],
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_edit_min_wallet")
async def prompt_edit_min_wallet(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.editing_min_wallet_amount)
    text = (
        "💰 <b>ویرایش حداقل مبلغ شارژ کیف پول:</b>\n\n"
        f"حداقل مبلغ فعلی: <b>{settings.MIN_WALLET_TOPUP_TOMAN:,} تومان</b>\n\n"
        "لطفاً <b>حداقل مبلغ جدید شارژ کیف پول</b> را به تومان وارد فرمایید:\n"
        "(مثال: <code>20000</code>)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_payments")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.editing_min_wallet_amount)
async def process_edit_min_wallet(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    digits = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
    if not digits.isdigit() or int(digits) <= 0:
        return await message.answer("⚠️ لطفاً یک رقم عددی معتبر به تومان وارد فرمایید:")
    amt = int(digits)
    settings.MIN_WALLET_TOPUP_TOMAN = amt
    update_env_variable("MIN_WALLET_TOPUP_TOMAN", str(amt))
    await state.clear()
    await message.answer(
        f"✅ <b>حداقل مبلغ شارژ کیف پول به {amt:,} تومان تغییر یافت.</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 مدیریت روش‌های پرداخت", callback_data="admin_manage_payments")],
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_manage_admins")
async def list_admin_access(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """List current authorized admin numerical IDs and allow adding or deleting any admin."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    static_ids = settings.ADMIN_TELEGRAM_IDS
    db_admins_res = await db_session.execute(
        select(User).where(User.role.in_([UserRole.SUPER_ADMIN, UserRole.ADMIN]))
    )
    db_admins = list(db_admins_res.scalars().all())

    # Map unique admin identifiers
    admin_dict = {}
    for aid in static_ids:
        admin_dict[aid] = None
    for da in db_admins:
        admin_dict[da.telegram_id] = da

    text = (
        "🔐 <b>سیستم امنیت و مدیریت دسترسی ادمین‌ها:</b>\n\n"
        "سامانه منحصراً به شناسه‌های زیر اجازه ورود به پنل مدیریت را می‌دهد.\n"
        "هیچ فرد دیگری دسترسی ندارد و دکمه پنل ادمین برای او نمایش داده نمی‌شود.\n\n"
        "👥 <b>لیست مدیران فعال در سیستم:</b>\n"
    )

    for tg_id, user_obj in admin_dict.items():
        is_self = (tg_id == db_user.telegram_id)
        self_tag = " <i>(اکانت شما)</i>" if is_self else ""
        u_tag = f"(@{user_obj.username})" if user_obj and user_obj.username else ""
        role_label = user_obj.role if user_obj else "admin"
        text += f"• <code>{tg_id}</code> {u_tag} — نقش: <b>{role_label}</b>{self_tag}\n"

    text += (
        "\n💡 <b>اقدامات مدیریت:</b>\n"
        "• برای افزودن مدیر جدید: دکمه <b>«➕ افزودن ادمین جدید»</b>\n"
        "• برای عزل و لغو دسترسی: دکمه <b>«🚫 عزل ادمین»</b>"
    )

    buttons = [
        [
            InlineKeyboardButton(text="➕ افزودن ادمین جدید", callback_data="admin_prompt_add_admin"),
            InlineKeyboardButton(text="🚫 عزل ادمین", callback_data="admin_prompt_demote_admin"),
        ]
    ]

    # Show a direct demote button for every admin EXCEPT the current user themselves
    for tg_id, user_obj in admin_dict.items():
        if tg_id != db_user.telegram_id:
            u_label = f"@{user_obj.username}" if user_obj and user_obj.username else str(tg_id)
            buttons.append([
                InlineKeyboardButton(text=f"🚫 عزل ادمین: {u_label}", callback_data=f"admin_ask_delete:{tg_id}")
            ])

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_prompt_demote_admin")
async def prompt_demote_admin(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Show dedicated menu for demoting/dismissing an admin."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    static_ids = settings.ADMIN_TELEGRAM_IDS
    db_admins_res = await db_session.execute(
        select(User).where(User.role.in_([UserRole.SUPER_ADMIN, UserRole.ADMIN]))
    )
    db_admins = list(db_admins_res.scalars().all())

    admin_dict = {}
    for aid in static_ids:
        admin_dict[aid] = None
    for da in db_admins:
        admin_dict[da.telegram_id] = da

    other_admins = {tg_id: u for tg_id, u in admin_dict.items() if tg_id != db_user.telegram_id}

    text = (
        "🚫 <b>منوی عزل مدیران:</b>\n\n"
        "برای عزل و لغو دسترسی هر یک از ادمین‌ها، می‌توانید روی دکمه مربوط به او کلیک کنید "
        "یا دکمه «✏️ ورود شناسه یا یوزرنیم» را انتخاب نمایید.\n\n"
    )

    buttons = []
    if other_admins:
        text += "👥 <b>انتخاب مستقیم مدیر جهت عزل:</b>\n"
        for tg_id, user_obj in other_admins.items():
            u_label = f"@{user_obj.username}" if user_obj and user_obj.username else f"شناسه {tg_id}"
            buttons.append([
                InlineKeyboardButton(text=f"🚫 عزل {u_label}", callback_data=f"admin_ask_delete:{tg_id}")
            ])
    else:
        text += "ℹ️ <i>در حال حاضر ادمین دیگری به غیر از شما در سیستم وجود ندارد.</i>\n"

    buttons.append([
        InlineKeyboardButton(text="✏️ ورود شناسه یا یوزرنیم جهت عزل", callback_data="admin_demote_by_input")
    ])
    buttons.append([
        InlineKeyboardButton(text="🔙 بازگشت به لیست مدیران", callback_data="admin_manage_admins")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "admin_demote_by_input")
async def prompt_demote_admin_input(callback: CallbackQuery, state: FSMContext, db_user: User):
    """Prompt current admin to enter numerical Telegram ID or @username to demote."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    await state.set_state(AdminState.entering_demote_admin_id)
    text = (
        "🚫 <b>عزل ادمین — ورود مشخصات:</b>\n\n"
        "لطفاً <b>شناسه عددی (Numeric ID)</b> یا <b>نام کاربری (@username)</b> ادمینی که قصد عزل او را دارید ارسال کنید:\n\n"
        "💡 مثال: <code>123456789</code> یا <code>@admin_support</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_admins")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.entering_demote_admin_id)
async def process_demote_admin_input(
    message: Message,
    state: FSMContext,
    db_user: User,
    db_session: AsyncSession,
):
    """Process numerical ID or @username for demoting an admin."""
    if not is_admin(db_user):
        return await message.answer("دسترسی غیرمجاز.")

    text_input = (message.text or "").strip()
    target_user = None
    target_tg_id = None

    if text_input.isdigit():
        target_tg_id = int(text_input)
        res = await db_session.execute(select(User).where(User.telegram_id == target_tg_id))
        target_user = res.scalar_one_or_none()
    elif text_input.startswith("@") or text_input.isalnum():
        clean_username = text_input.lstrip("@").lower()
        res = await db_session.execute(
            select(User).where(func.lower(User.username) == clean_username)
        )
        target_user = res.scalar_one_or_none()
        if target_user:
            target_tg_id = target_user.telegram_id

    if not target_tg_id and not target_user:
        return await message.answer(
            "❌ کاربری با این مشخصات یافت نشد.\n"
            "لطفاً شناسه عددی معتبر یا نام کاربری دقیق ارسال نمایید:"
        )

    if target_tg_id == db_user.telegram_id:
        await state.clear()
        return await message.answer(
            "⛔️ شما نمی‌توانید اکانت خودتان را عزل کنید!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 بازگشت به لیست مدیران", callback_data="admin_manage_admins")]
            ])
        )

    # 1. Update in DB
    if target_user:
        target_user.role = UserRole.CUSTOMER
        await db_session.commit()

    # 2. Remove from settings and .env
    if target_tg_id and target_tg_id in settings.ADMIN_TELEGRAM_IDS:
        settings.ADMIN_TELEGRAM_IDS = [x for x in settings.ADMIN_TELEGRAM_IDS if x != target_tg_id]
        sync_admin_ids_to_env()

    await state.clear()

    u_tag = f"(@{html.escape(target_user.username)})" if target_user and target_user.username else ""
    success_text = (
        f"✅ <b>مدیر <code>{target_tg_id}</code> {u_tag} با موفقیت عزل شد!</b>\n\n"
        f"دسترسی مدیریت این کاربر به طور کامل لغو گردید و به کاربر عادی تنزل یافت."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 بازگشت به لیست مدیران", callback_data="admin_manage_admins")],
        [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
    ])
    await message.answer(success_text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_ask_delete:"))
async def ask_delete_admin(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Ask for confirmation before deleting/demoting an admin."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    target_tg_id = int(callback.data.split(":")[1])
    if target_tg_id == db_user.telegram_id:
        return await callback.answer("شما نمی‌توانید اکانت خودتان را عزل کنید.", show_alert=True)

    user_res = await db_session.execute(select(User).where(User.telegram_id == target_tg_id))
    target_user = user_res.scalar_one_or_none()
    u_label = f"@{html.escape(target_user.username)} ({target_tg_id})" if target_user and target_user.username else str(target_tg_id)

    text = (
        f"⚠️ <b>تایید عزل دسترسی مدیر:</b>\n\n"
        f"آیا اطمینان دارید که می‌خواهید دسترسی مدیریت کاربر <b>{u_label}</b> را به طور کامل لغو و او را عزل کنید؟\n\n"
        f"این کاربر بلافاصله به کاربر عادی تنزل یافته و منوی مدیریت برای او قفل خواهد شد."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚫 بله، عزل شود", callback_data=f"admin_do_delete:{target_tg_id}"),
            InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_admins"),
        ]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_do_delete:"))
@router.callback_query(F.data.startswith("admin_demote:"))
async def do_delete_admin(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Execute deletion/demotion of admin from DB, memory, and .env file."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    raw_arg = callback.data.split(":")[1]
    # Handle either telegram_id int or User UUID
    target_user = None
    target_tg_id = None
    try:
        target_tg_id = int(raw_arg)
        user_res = await db_session.execute(select(User).where(User.telegram_id == target_tg_id))
        target_user = user_res.scalar_one_or_none()
    except ValueError:
        # UUID
        target_id = uuid.UUID(raw_arg)
        target_user = await db_session.get(User, target_id)
        if target_user:
            target_tg_id = target_user.telegram_id

    if target_tg_id == db_user.telegram_id:
        return await callback.answer("شما نمی‌توانید اکانت خودتان را عزل کنید.", show_alert=True)

    # 1. Update in DB
    if target_user:
        target_user.role = UserRole.CUSTOMER
        await db_session.commit()

    # 2. Remove from settings and .env
    if target_tg_id and target_tg_id in settings.ADMIN_TELEGRAM_IDS:
        settings.ADMIN_TELEGRAM_IDS = [x for x in settings.ADMIN_TELEGRAM_IDS if x != target_tg_id]
        sync_admin_ids_to_env()

    await callback.answer(f"کاربر {target_tg_id or ''} با موفقیت عزل شد و دسترسی مدیریت لغو گردید.", show_alert=True)
    return await list_admin_access(callback, db_user, db_session)


demote_admin_handler = do_delete_admin


@router.callback_query(F.data == "admin_prompt_add_admin")
async def prompt_add_admin_id(callback: CallbackQuery, state: FSMContext, db_user: User):
    """Prompt current admin to enter the new user's numerical Telegram ID or @username."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    await state.set_state(AdminState.entering_new_admin_id)
    text = (
        "➕ <b>افزودن ادمین جدید به سیستم:</b>\n\n"
        "لطفاً <b>شناسه عددی تلگرام</b> یا <b>نام کاربری (@username)</b> فرد مورد نظر را ارسال فرمایید:\n\n"
        "📌 <i>مثال شناسه عددی:</i> <code>582910444</code>\n"
        "📌 <i>مثال نام کاربری:</i> <code>@my_admin</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_manage_admins")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.entering_new_admin_id)
async def process_new_admin_id(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    """Process numerical ID or @username, find/create user, promote to ADMIN role and sync to .env."""
    if not is_admin(db_user):
        await state.clear()
        return

    query = (message.text or "").strip().replace(" ", "")
    target_user = None

    if query.startswith("@") or not query.isdigit():
        uname = query.lstrip("@").lower()
        res = await db_session.execute(select(User).where(User.username.ilike(uname)))
        target_user = res.scalar_one_or_none()
        if not target_user:
            return await message.answer(
                f"❌ <b>کاربری با نام کاربری @{uname} در سیستم یافت نشد.</b>\n\n"
                "کاربر برای شناسایی با نام کاربری باید حداقل یکبار دکمه /start ربات را زده باشد، "
                "یا می‌توانید مستقیماً شناسه عددی او را وارد فرمایید:",
                parse_mode="HTML"
            )
        target_user.role = UserRole.ADMIN
        target_tg_id = target_user.telegram_id
    else:
        clean_digits = query.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
        if not clean_digits.isdigit() or int(clean_digits) <= 0:
            return await message.answer(
                "⚠️ لطفاً یک شناسه عددی معتبر (مثال: <code>582910444</code>) یا نام کاربری (مثال: <code>@username</code>) ارسال فرمایید:",
                parse_mode="HTML"
            )

        target_tg_id = int(clean_digits)
        user_res = await db_session.execute(select(User).where(User.telegram_id == target_tg_id))
        target_user = user_res.scalar_one_or_none()

        if not target_user:
            import secrets
            from app.database.models.user import Wallet
            target_user = User(
                id=uuid.uuid4(),
                telegram_id=target_tg_id,
                role=UserRole.ADMIN,
                referral_code=secrets.token_hex(4).upper(),
            )
            db_session.add(target_user)
            await db_session.flush()
            wallet = Wallet(user_id=target_user.id, balance_irr=0)
            db_session.add(wallet)
        else:
            target_user.role = UserRole.ADMIN

    # Sync with settings and .env
    if target_tg_id not in settings.ADMIN_TELEGRAM_IDS:
        settings.ADMIN_TELEGRAM_IDS.append(target_tg_id)
        sync_admin_ids_to_env()

    await db_session.commit()
    await state.clear()

    u_tag = f"(@{html.escape(target_user.username)})" if target_user.username else ""
    text = (
        f"✅ <b>کاربر <code>{target_tg_id}</code> {u_tag} با موفقیت به عنوان ادمین ثبت شد!</b>\n\n"
        f"از این پس این کاربر با ارسال دستور <code>/start</code> یا <code>/admin</code> به پنل مدیریت دسترسی دارد.\n"
        f"هیچ کاربر متفرقه‌ای دسترسی نخواهد داشت."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 بازگشت به لیست مدیران", callback_data="admin_manage_admins")],
        [InlineKeyboardButton(text="⚙️ بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_prompt_wallet_charge")
async def prompt_wallet_charge(callback: CallbackQuery, state: FSMContext, db_user: User):
    """Prompt admin to enter user Telegram ID or @username to adjust wallet balance."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    await state.set_state(AdminState.entering_user_id_search)
    text = (
        "💳 <b>شارژ و ویرایش اعتبار کیف پول کاربر:</b>\n\n"
        "لطفاً <b>شناسه عددی تلگرام</b> یا <b>نام کاربری (@username)</b> کاربر مورد نظر را ارسال فرمایید:\n\n"
        "📌 <i>مثال شناسه عددی:</i> <code>123456789</code>\n"
        "📌 <i>مثال نام کاربری:</i> <code>@my_username</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_refresh_dashboard")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.entering_user_id_search)
async def process_wallet_user_search(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    """Search user by numerical Telegram ID or @username and prompt for amount."""
    if not is_admin(db_user):
        await state.clear()
        return

    query = (message.text or "").strip()
    target_user = None

    if query.startswith("@"):
        uname = query.lstrip("@").lower()
        res = await db_session.execute(select(User).where(User.username.ilike(uname)))
        target_user = res.scalar_one_or_none()
    else:
        clean_digits = query.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))
        if clean_digits.isdigit():
            tg_id = int(clean_digits)
            res = await db_session.execute(select(User).where(User.telegram_id == tg_id))
            target_user = res.scalar_one_or_none()

    if not target_user:
        import html
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_refresh_dashboard")]
        ])
        return await message.answer(
            f"❌ <b>کاربری با مشخصه «{html.escape(query)}» در سیستم یافت نشد.</b>\n\n"
            "لطفاً مجدداً شناسه عددی یا نام کاربری را با دقت وارد فرمایید:",
            reply_markup=kb,
            parse_mode="HTML",
        )

    wallet_svc = WalletService(db_session)
    wallet = await wallet_svc.get_or_create_wallet(target_user.id)
    balance_toman = wallet.balance_toman

    await state.set_state(AdminState.entering_balance_adjustment)
    await state.update_data(
        target_user_id=str(target_user.id),
        target_tg_id=target_user.telegram_id,
        target_username=target_user.username,
    )

    active_subs_count = (await db_session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.user_id == target_user.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )).scalar_one()

    u_tag = f"@{target_user.username}" if target_user.username else "ندارد"
    first_name = target_user.first_name or "کاربر"
    role_fa = "💼 نماینده (Reseller)" if target_user.role == UserRole.RESELLER else ("👑 مدیر" if target_user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN) else "👤 مشتری عادی")
    status_fa = "🔴 مسدود (Banned)" if target_user.is_banned else "🟢 فعال"

    text = (
        f"👤 <b>مشخصات کاربر پیدا شد (مدیریت پیشرفته کاربر):</b>\n\n"
        f"• نام: <b>{first_name}</b>\n"
        f"• شناسه عددی: <code>{target_user.telegram_id}</code>\n"
        f"• نام کاربری: {u_tag}\n"
        f"• سطح دسترسی: <b>{role_fa}</b>\n"
        f"• وضعیت حساب: <b>{status_fa}</b>\n"
        f"• اشتراک‌های فعال: <b>{active_subs_count} عدد</b>\n"
        f"• موجودی کیف پول: <b>{balance_toman:,} تومان</b> ({wallet.balance_irr:,} ریال)\n\n"
        f"جهت مدیریت حساب، یکی از عملیات زیر را انتخاب فرمایید:"
    )

    reseller_btn_text = "👤 تبدیل به کاربر عادی" if target_user.role == UserRole.RESELLER else "💼 ارتقا به نماینده (Reseller)"
    ban_btn_text = "🟢 رفع مسدودیت (Unban)" if target_user.is_banned else "🚫 مسدود کردن (Ban)"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 شارژ / کسر موجودی کیف پول", callback_data=f"admin_user_charge:{target_user.id}"),
        ],
        [
            InlineKeyboardButton(text=reseller_btn_text, callback_data=f"admin_toggle_reseller:{target_user.id}"),
            InlineKeyboardButton(text=ban_btn_text, callback_data=f"admin_toggle_ban:{target_user.id}"),
        ],
        [
            InlineKeyboardButton(text="✉️ ارسال پیام خصوصی به پی‌وی", callback_data=f"admin_send_pm:{target_user.id}"),
        ],
        [
            InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard"),
        ],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.message(AdminState.entering_balance_adjustment)
async def process_wallet_balance_adjustment(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    """Adjust user wallet balance, record double-entry accounting, and notify customer."""
    if not is_admin(db_user):
        await state.clear()
        return

    # If user sent any slash command, cancel state and handle command
    msg_text = (message.text or "").strip()
    if msg_text.startswith("/"):
        await state.clear()
        if msg_text.startswith("/start"):
            from app.bot.handlers.start import handle_start
            from aiogram.filters import CommandObject
            cmd = CommandObject(prefix="/", command="start", args="")
            return await handle_start(message, cmd, state, db_user, db_session)
        elif msg_text.startswith("/admin"):
            return await open_admin_panel(message, state, db_user, db_session)
        return await message.answer("عملیات تغییر موجودی لغو شد.")

    data = await state.get_data()
    target_user_id_str = data.get("target_user_id")
    target_tg_id = data.get("target_tg_id")

    if not target_user_id_str:
        await state.clear()
        return await message.answer("⚠️ نشست تغییر اعتبار منقضی شده است.")

    target_user_id = uuid.UUID(target_user_id_str)
    target_user = await db_session.get(User, target_user_id)
    if not target_user:
        await state.clear()
        return await message.answer("⚠️ کاربر مورد نظر در دیتابیس یافت نشد.")

    raw_text = (message.text or "").strip().replace(",", "").replace("،", "").replace(" ", "")
    is_negative = raw_text.startswith("-")
    clean_digits = raw_text.lstrip("-").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧۸۹", "01234567890123456789"))

    if not clean_digits.isdigit() or int(clean_digits) == 0:
        return await message.answer(
            "⚠️ لطفاً یک رقم عددی معتبر به تومان ارسال فرمایید (مثال: `50000` یا `-20000`):"
        )

    amount_toman = int(clean_digits)
    amount_irr = amount_toman * 10
    wallet_svc = WalletService(db_session)

    try:
        if is_negative:
            new_bal_irr = await wallet_svc.debit(
                user_id=target_user.id,
                amount_irr=amount_irr,
                order_id=f"ADMIN_DEBIT_{uuid.uuid4().hex[:8]}",
                reason="ADMIN_ADJUSTMENT",
            )
            action_desc = f"🔻 مبلغ <b>{amount_toman:,} تومان</b> از کیف پول کاربر کسر شد."
            customer_notify = (
                f"⚠️ <b>بروزرسانی موجودی کیف پول:</b>\n\n"
                f"مبلغ <b>{amount_toman:,} تومان</b> از کیف پول شما توسط مدیریت کسر گردید.\n"
                f"💳 موجودی فعلی شما: <b>{new_bal_irr // 10:,} تومان</b>"
            )
        else:
            new_bal_irr = await wallet_svc.credit(
                user_id=target_user.id,
                amount_irr=amount_irr,
                payment_id=f"ADMIN_CREDIT_{uuid.uuid4().hex[:8]}",
                reason="ADMIN_TOPUP",
            )
            action_desc = f"🎉 مبلغ <b>+{amount_toman:,} تومان</b> با موفقیت به کیف پول کاربر اضافه شد."
            customer_notify = (
                f"🎉 <b>کیف پول شما با موفقیت شارژ شد!</b>\n\n"
                f"💰 مبلغ <b>{amount_toman:,} تومان</b> توسط مدیریت به موجودی حساب شما افزوده شد.\n"
                f"💳 موجودی جدید شما: <b>{new_bal_irr // 10:,} تومان</b>\n\n"
                f"شما می‌توانید با استفاده از این موجودی به صورت آنی اقدام به خرید یا تمدید کانفیگ‌ها فرمایید."
            )

        await db_session.commit()
        await state.clear()

        # Send Telegram notification to the customer
        if target_tg_id:
            try:
                await bot.send_message(
                    chat_id=target_tg_id,
                    text=customer_notify,
                    parse_mode="HTML",
                )
            except Exception as notify_err:
                logger.warning(f"Could not send wallet notification to user {target_tg_id}: {notify_err}")

        # Send response to Admin
        admin_response = (
            f"✅ <b>عملیات تغییر موجودی انجام شد!</b>\n\n"
            f"👤 کاربر: <code>{target_tg_id}</code>\n"
            f"{action_desc}\n"
            f"💳 موجودی جدید کیف پول: <b>{new_bal_irr // 10:,} تومان</b> ({new_bal_irr:,} ریال)\n\n"
            f"پیامک اطلاع‌رسانی شارژ نیز به تلگرام کاربر ارسال شد."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 شارژ کاربر دیگر", callback_data="admin_prompt_wallet_charge")],
            [InlineKeyboardButton(text="⚙️ بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
        ])
        await message.answer(admin_response, reply_markup=kb, parse_mode="HTML")

    except Exception as exc:
        logger.error(f"Error adjusting wallet balance for user {target_user.id}: {exc}", exc_info=True)
        await state.clear()
        await message.answer(f"❌ خطا در تغییر موجودی: {str(exc)}")


# ==========================================
# ADVANCED USER MANAGEMENT (LIST ALL / BAN / RESELLER / DIRECT PM)
# ==========================================

@router.callback_query(F.data.startswith("admin_view_all_users"))
async def list_all_users_admin(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Paginated list of all registered users with detailed breakdown of what subscriptions, locations, and balances they have."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    PAGE_SIZE = 4

    total_count_res = await db_session.execute(select(func.count(User.id)))
    total_users = total_count_res.scalar() or 0

    if total_users == 0:
        return await callback.answer("هیچ کاربری در سیستم ثبت نشده است.", show_alert=True)

    total_pages = max(1, (total_users + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    users_res = await db_session.execute(
        select(User)
        .order_by(User.created_at.desc())
        .offset(page * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    users = list(users_res.scalars().all())

    import html
    from app.database.models.user import Wallet
    from app.database.models.subscription import Subscription
    from app.database.models.panel import Server
    from app.core.constants import UserRole, SubscriptionStatus
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    text = (
        f"👥 <b>لیست جامع کاربران و وضعیت سرویس‌ها (صفحه {page + 1} از {total_pages})</b>\n"
        f"📊 کل اعضای ثبت‌شده در ربات: <b>{total_users} نفر</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    buttons = []

    for idx, u in enumerate(users, start=(page * PAGE_SIZE) + 1):
        # Wallet balance
        w_res = await db_session.execute(select(Wallet).where(Wallet.user_id == u.id))
        w = w_res.scalar_one_or_none()
        bal_toman = w.balance_toman if w else 0

        # Subscriptions
        subs_res = await db_session.execute(
            select(Subscription)
            .where(Subscription.user_id == u.id)
            .order_by(Subscription.created_at.desc())
        )
        subs = list(subs_res.scalars().all())

        u_name = html.escape(u.first_name or "کاربر")
        u_tag = f"@{html.escape(u.username)}" if u.username else "بدون یوزرنیم"
        
        role_fa = "👑 مدیر ارشد" if u.role == UserRole.SUPER_ADMIN else ("👑 مدیر" if u.role == UserRole.ADMIN else ("💼 نماینده" if u.role == UserRole.RESELLER else "👤 مشتری عادی"))
        ban_tag = " ⛔️ [مسدود]" if u.is_banned else ""

        text += f"<b>{idx}. {u_name}</b> ({u_tag}){ban_tag}\n"
        text += f"   🆔 شناسه: <code>{u.telegram_id}</code> | رول: <b>{role_fa}</b>\n"
        text += f"   💰 کیف پول: <b>{bal_toman:,} تومان</b>\n"

        if not subs:
            text += "   📦 اشتراک‌ها: <i>فاقد سرویس فعال</i>\n"
        else:
            text += f"   📦 اشتراک‌ها (<b>{len(subs)} مورد</b>):\n"
            for s_idx, s in enumerate(subs, 1):
                srv = await db_session.get(Server, s.server_id) if s.server_id else None
                loc_name = srv.location_name if srv else "نامشخص"
                flag = srv.flag_emoji if srv else "🌐"
                proto = (s.protocol or "VLESS").upper()

                used_gb = (s.traffic_used_bytes or 0) / (1024 ** 3)
                limit_gb = (s.traffic_limit_bytes or 0) / (1024 ** 3)

                if s.expires_at:
                    exp_dt = s.expires_at
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    diff = exp_dt - now
                    days_left = max(0, diff.days)
                    exp_str = f"{days_left} روز مانده" if days_left > 0 else "منقضی شده"
                else:
                    exp_str = "نامحدود"

                status_icon = "🟢" if s.status == SubscriptionStatus.ACTIVE else ("🟡" if s.status == SubscriptionStatus.SUSPENDED else "🔴")
                status_fa = "فعال" if s.status == SubscriptionStatus.ACTIVE else ("معلق" if s.status == SubscriptionStatus.SUSPENDED else "منقضی")

                text += f"      ▫️ {flag} <b>{loc_name}</b> ({proto}) | {status_icon} <b>{status_fa}</b>\n"
                text += f"         📊 مصرف: <b>{used_gb:.1f}</b> از <b>{limit_gb:.1f} GB</b> | ⏳ زمان: <b>{exp_str}</b>\n"

        text += "────────────────────\n"

        # Action button to open user detail card
        btn_label = f"⚙️ مدیریت {u.first_name or u.telegram_id} ({u.telegram_id})"
        buttons.append([InlineKeyboardButton(text=btn_label, callback_data=f"admin_user_detail:{u.id}")])

    # Pagination row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ صفحه قبل", callback_data=f"admin_view_all_users:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"📄 {page + 1} از {total_pages}", callback_data="ignore_action"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="صفحه بعد ▶️", callback_data=f"admin_view_all_users:{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton(text="🔍 جستجوی کاربر با آیدی", callback_data="admin_prompt_wallet_charge"),
        InlineKeyboardButton(text="⚙️ بازگشت به داشبورد", callback_data="admin_refresh_dashboard"),
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_user_detail:"))
async def show_admin_user_detail(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    """Show comprehensive detail card for a selected user with quick actions."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    user_id_str = callback.data.split(":")[1]
    target_user_id = uuid.UUID(user_id_str)
    target_user = await db_session.get(User, target_user_id)
    if not target_user:
        return await callback.answer("کاربر یافت نشد.", show_alert=True)

    await state.clear()

    wallet_svc = WalletService(db_session)
    wallet = await wallet_svc.get_or_create_wallet(target_user.id)
    balance_toman = wallet.balance_toman

    subs_res = await db_session.execute(
        select(Subscription).where(Subscription.user_id == target_user.id).order_by(Subscription.created_at.desc())
    )
    subs = list(subs_res.scalars().all())

    import html
    from app.core.constants import UserRole, SubscriptionStatus
    from app.database.models.panel import Server
    from datetime import datetime, timezone

    u_tag = f"@{html.escape(target_user.username)}" if target_user.username else "ندارد"
    first_name = html.escape(target_user.first_name or "کاربر")
    role_fa = "💼 نماینده (Reseller)" if target_user.role == UserRole.RESELLER else ("👑 مدیر" if target_user.role in (UserRole.ADMIN, UserRole.SUPER_ADMIN) else "👤 مشتری عادی")
    status_fa = "🔴 مسدود (Banned)" if target_user.is_banned else "🟢 فعال"

    now = datetime.now(timezone.utc)
    text = (
        f"👤 <b>مدیریت پیشرفته حساب کاربر:</b>\n\n"
        f"• نام: <b>{first_name}</b>\n"
        f"• شناسه عددی: <code>{target_user.telegram_id}</code>\n"
        f"• نام کاربری: {u_tag}\n"
        f"• سطح دسترسی: <b>{role_fa}</b>\n"
        f"• وضعیت حساب: <b>{status_fa}</b>\n"
        f"• موجودی کیف پول: <b>{balance_toman:,} تومان</b> ({wallet.balance_irr:,} ریال)\n"
        f"• تعداد اشتراک‌ها: <b>{len(subs)} عدد</b>\n\n"
    )

    if subs:
        text += "<b>📦 جزئیات سرویس‌ها و لوکیشن‌های این کاربر:</b>\n"
        for idx, s in enumerate(subs, 1):
            srv = await db_session.get(Server, s.server_id) if s.server_id else None
            loc_name = srv.location_name if srv else "نامشخص"
            flag = srv.flag_emoji if srv else "🌐"
            proto = (s.protocol or "VLESS").upper()
            used_gb = (s.traffic_used_bytes or 0) / (1024 ** 3)
            limit_gb = (s.traffic_limit_bytes or 0) / (1024 ** 3)
            if s.expires_at:
                exp_dt = s.expires_at
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                diff = exp_dt - now
                days_left = max(0, diff.days)
                exp_str = f"{days_left} روز مانده" if days_left > 0 else "منقضی شده"
            else:
                exp_str = "نامحدود"
            status_fa_sub = "🟢 فعال" if s.status == SubscriptionStatus.ACTIVE else "🔴 غیرفعال"
            text += (
                f"  {idx}) {flag} <b>{loc_name}</b> ({proto}) | {status_fa_sub}\n"
                f"     🏷 ایمیل: <code>{s.client_email}</code>\n"
                f"     📊 مصرف: <b>{used_gb:.1f}</b> از <b>{limit_gb:.1f} GB</b>\n"
                f"     ⏳ زمان: <b>{exp_str}</b>\n"
            )
        text += "\n"

    text += "جهت انجام عملیات، یکی از دکمه‌های زیر را انتخاب کنید یا مبلغی برای شارژ/کسر تایپ فرمایید:"

    reseller_btn_text = "👤 تبدیل به کاربر عادی" if target_user.role == UserRole.RESELLER else "💼 ارتقا به نماینده (Reseller)"
    ban_btn_text = "🟢 رفع مسدودیت (Unban)" if target_user.is_banned else "🚫 مسدود کردن (Ban)"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 شارژ / کسر موجودی کیف پول", callback_data=f"admin_user_charge:{target_user.id}"),
        ],
        [
            InlineKeyboardButton(text=reseller_btn_text, callback_data=f"admin_toggle_reseller:{target_user.id}"),
            InlineKeyboardButton(text=ban_btn_text, callback_data=f"admin_toggle_ban:{target_user.id}"),
        ],
        [
            InlineKeyboardButton(text="✉️ ارسال پیام خصوصی به پی‌وی", callback_data=f"admin_send_pm:{target_user.id}"),
        ],
        [
            InlineKeyboardButton(text="👥 بازگشت به لیست کل کاربران", callback_data="admin_view_all_users:0"),
            InlineKeyboardButton(text="⚙️ بازگشت به داشبورد", callback_data="admin_refresh_dashboard"),
        ],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("admin_user_charge:"))
async def prompt_user_charge_amount(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    target_user_id = uuid.UUID(callback.data.split(":")[1])
    target_user = await db_session.get(User, target_user_id)
    if not target_user:
        return await callback.answer("کاربر یافت نشد", show_alert=True)

    await state.set_state(AdminState.entering_balance_adjustment)
    await state.update_data(
        target_user_id=str(target_user.id),
        target_tg_id=target_user.telegram_id,
        target_username=target_user.username or "",
    )
    text = (
        f"💰 <b>شارژ یا کسر موجودی برای کاربر <code>{target_user.telegram_id}</code>:</b>\n\n"
        f"لطفاً مبلغ مورد نظر را به تومان وارد فرمایید:\n"
        f"• برای <b>شارژ</b>: عدد مثبت (مثال: <code>50000</code>)\n"
        f"• برای <b>کسر</b>: عدد منفی (مثال: <code>-20000</code>)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_refresh_dashboard")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_toggle_reseller:"))
async def toggle_user_reseller(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    target_user_id = uuid.UUID(callback.data.split(":")[1])
    target_user = await db_session.get(User, target_user_id)
    if not target_user:
        return await callback.answer("کاربر یافت نشد", show_alert=True)

    if target_user.role == UserRole.RESELLER:
        target_user.role = UserRole.CUSTOMER
        status_msg = "کاربر به مشتری عادی تبدیل شد."
    else:
        target_user.role = UserRole.RESELLER
        status_msg = f"کاربر با موفقیت به نماینده فروش (با {settings.RESELLER_DISCOUNT_PERCENT}٪ تخفیف) ارتقا یافت!"

    await db_session.commit()
    await callback.answer(f"✅ {status_msg}", show_alert=True)
    await refresh_admin_dashboard(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_toggle_ban:"))
async def toggle_user_ban(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    target_user_id = uuid.UUID(callback.data.split(":")[1])
    target_user = await db_session.get(User, target_user_id)
    if not target_user:
        return await callback.answer("کاربر یافت نشد", show_alert=True)

    target_user.is_banned = not target_user.is_banned
    status_msg = "کاربر مسدود (Ban) شد." if target_user.is_banned else "مسدودیت کاربر رفع شد (Unban)."
    await db_session.commit()
    await callback.answer(f"✅ {status_msg}", show_alert=True)
    await refresh_admin_dashboard(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_send_pm:"))
async def prompt_direct_pm_to_user(callback: CallbackQuery, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    target_user_id = uuid.UUID(callback.data.split(":")[1])
    target_user = await db_session.get(User, target_user_id)
    if not target_user:
        return await callback.answer("کاربر یافت نشد", show_alert=True)

    await state.set_state(AdminState.entering_direct_pm_message)
    await state.update_data(target_tg_id=target_user.telegram_id)
    text = (
        f"✉️ <b>ارسال پیام اختصاصی به پی‌وی کاربر <code>{target_user.telegram_id}</code>:</b>\n\n"
        f"لطفاً متن پیام مورد نظر را بنویسید و ارسال کنید:\n\n"
        f"<i>این پیام مستقیماً از طرف ربات برای کاربر ارسال خواهد شد.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_refresh_dashboard")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.message(AdminState.entering_direct_pm_message)
async def process_admin_direct_pm(message: Message, state: FSMContext, bot: Bot, db_user: User):
    if not is_admin(db_user):
        await state.clear()
        return
    data = await state.get_data()
    target_tg_id = data.get("target_tg_id")
    await state.clear()

    text_to_send = message.text or message.caption
    if not text_to_send:
        return await message.answer("⚠️ لطفاً متنی برای ارسال وارد فرمایید.")

    pm_content = (
        f"📩 <b>پیام از طرف تیم مدیریت VPN Private:</b>\n\n"
        f"{text_to_send}\n\n"
        f"💬 <i>در صورت نیاز به پاسخ، می‌توانید از بخش پشتیبانی تیکت ارسال نمایید.</i>"
    )
    try:
        if message.photo:
            try:
                await bot.send_photo(chat_id=target_tg_id, photo=message.photo[-1].file_id, caption=pm_content, parse_mode="HTML")
            except Exception:
                safe_pm = f"📩 <b>پیام از طرف تیم مدیریت VPN Private:</b>\n\n{html.escape(text_to_send)}\n\n💬 <i>در صورت نیاز به پاسخ، می‌توانید از بخش پشتیبانی تیکت ارسال نمایید.</i>"
                await bot.send_photo(chat_id=target_tg_id, photo=message.photo[-1].file_id, caption=safe_pm, parse_mode="HTML")
        else:
            try:
                await bot.send_message(chat_id=target_tg_id, text=pm_content, parse_mode="HTML")
            except Exception:
                safe_pm = f"📩 <b>پیام از طرف تیم مدیریت VPN Private:</b>\n\n{html.escape(text_to_send)}\n\n💬 <i>در صورت نیاز به پاسخ، می‌توانید از بخش پشتیبانی تیکت ارسال نمایید.</i>"
                await bot.send_message(chat_id=target_tg_id, text=safe_pm, parse_mode="HTML")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")]
        ])
        await message.answer(f"✅ <b>پیام شما با موفقیت به کاربر <code>{target_tg_id}</code> تحویل داده شد!</b>", reply_markup=kb, parse_mode="HTML")
    except Exception as exc:
        await message.answer(f"❌ خطا در ارسال پیام به کاربر: {exc}")


# ==========================================
# SUBSCRIPTION MANAGEMENT (TRAFFIC / DAYS / RESET)
# ==========================================

@router.callback_query(F.data == "admin_manage_subs")
async def list_admin_subscriptions(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    subs_res = await db_session.execute(
        select(Subscription).order_by(Subscription.created_at.desc()).limit(15)
    )
    subs = list(subs_res.scalars().all())

    kb_rows = []
    for s in subs:
        st_icon = "🟢" if s.status == SubscriptionStatus.ACTIVE else "🔴"
        email_short = s.client_email.split("@")[0]
        kb_rows.append([
            InlineKeyboardButton(
                text=f"{st_icon} {email_short} ({s.protocol.upper()})",
                callback_data=f"admin_sub_detail:{s.id}"
            )
        ])
    kb_rows.append([InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")])

    text = (
        f"🛠 <b>مدیریت اشتراک‌ها و اکانت‌ها ({len(subs)} مورد اخیر):</b>\n\n"
        "جهت مشاهده جزئیات، افزایش حجم، تمدید روزها، قطع/وصل یا دریافت کانفیگ و QR، روی اشتراک مورد نظر کلیک فرمایید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_sub_detail:"))
async def view_admin_sub_detail(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    sub_id = uuid.UUID(callback.data.split(":")[1])
    sub = await db_session.get(Subscription, sub_id)
    if not sub:
        return await callback.answer("اشتراک یافت نشد", show_alert=True)

    server = await db_session.get(Server, sub.server_id)
    user = await db_session.get(User, sub.user_id)

    status_icon = "🟢 فعال" if sub.status == SubscriptionStatus.ACTIVE else "🔴 قطع / غیرفعال"
    used_gb = sub.traffic_used_bytes / (1024**3)
    limit_gb = f"{sub.traffic_limit_bytes / (1024**3):.1f}" if sub.traffic_limit_bytes > 0 else "نامحدود"
    expiry_str = sub.expires_at.strftime("%Y/%m/%d %H:%M") if sub.expires_at else "نامحدود"

    text = (
        f"🛠 <b>جزئیات اشتراک VPN:</b>\n\n"
        f"👤 کاربر: <b>{user.first_name if user else 'نامشخص'}</b> (<code>{user.telegram_id if user else 'ندارد'}</code>)\n"
        f"📧 ایمیل/شناسه: <code>{sub.client_email}</code>\n"
        f"🌍 سرور: <b>{server.flag_emoji if server else ''} {server.location_name if server else 'نامشخص'}</b>\n"
        f"🔒 پروتکل: <b>{sub.protocol.upper()}</b>\n"
        f"📊 مصرف ترافیک: <b>{used_gb:.2f} از {limit_gb} گیگابایت</b>\n"
        f"⏳ تاریخ انقضا: <code>{expiry_str}</code>\n"
        f"🔐 وضعیت: <b>{status_icon}</b>\n\n"
        f"عملیات مورد نظر خود را انتخاب فرمایید:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ +۱۰ گیگ حجم", callback_data=f"admin_sub_add_traffic:{sub.id}:10"),
            InlineKeyboardButton(text="➕ +۲۰ گیگ حجم", callback_data=f"admin_sub_add_traffic:{sub.id}:20"),
        ],
        [
            InlineKeyboardButton(text="⏳ +۱۵ روز تمدید", callback_data=f"admin_sub_add_days:{sub.id}:15"),
            InlineKeyboardButton(text="⏳ +۳۰ روز تمدید", callback_data=f"admin_sub_add_days:{sub.id}:30"),
        ],
        [
            InlineKeyboardButton(
                text="🔴 قطع موقت" if sub.status == SubscriptionStatus.ACTIVE else "🟢 فعال‌سازی مجدد",
                callback_data=f"admin_sub_toggle_status:{sub.id}"
            ),
            InlineKeyboardButton(text="🔄 بازنشانی لینک ساب", callback_data=f"admin_sub_reset_sub:{sub.id}"),
        ],
        [
            InlineKeyboardButton(text="📱 دریافت کانفیگ و QR", callback_data=f"admin_sub_get_qr:{sub.id}"),
        ],
        [
            InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_manage_subs"),
        ],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


async def _sync_sub_to_remote_panel(sub: Subscription, db_session: AsyncSession) -> tuple[bool, str]:
    """Sync subscription state, traffic limit, expiry, enable status and sub token to 3X-UI panel."""
    try:
        panel = await db_session.get(Panel, sub.panel_id)
        inbound = await db_session.get(Inbound, sub.inbound_id)
        if not panel or not inbound:
            return False, "سرور یا اینباند متصل به اشتراک یافت نشد."
        if not panel.is_active:
            return False, f"پنل سرور {panel.name} غیرفعال است."

        from app.integrations.xui.client import ThreeXUIClient
        from app.integrations.xui.adapter import ThreeXUIPanelAdapter
        from app.integrations.xui.schemas import ClientSpec

        client = ThreeXUIClient(
            base_url=panel.base_url,
            username=panel.username,
            password=panel.password,
            api_token=panel.api_token,
            login_secret=panel.login_secret,
        )
        adapter = ThreeXUIPanelAdapter(client)
        remote_inb = getattr(inbound, "remote_inbound_id", getattr(inbound, "panel_inbound_id", 1))
        expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
        spec = ClientSpec(
            id=sub.client_uuid,
            email=sub.client_email,
            flow="xtls-rprx-vision" if (sub.protocol or "").lower() == "vless" else "",
            limitIp=inbound.max_clients or 2,
            totalGB=int(sub.traffic_limit_bytes or 0),
            expiryTime=expiry_ms,
            enable=(sub.status == SubscriptionStatus.ACTIVE),
            subId=sub.subscription_token,
        )
        success = await adapter.update_client(remote_inb, sub.client_uuid, spec)
        return success, ""
    except Exception as exc:
        logger.warning(f"Failed to sync subscription {sub.id} to panel: {exc}")
        return False, str(exc)


@router.callback_query(F.data.startswith("admin_sub_add_traffic:"))
async def admin_sub_add_traffic(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    parts = callback.data.split(":")
    sub_id = uuid.UUID(parts[1])
    extra_gb = int(parts[2])

    sub = await db_session.get(Subscription, sub_id)
    if not sub:
        return await callback.answer("اشتراک یافت نشد", show_alert=True)

    extra_bytes = extra_gb * (1024**3)
    if sub.traffic_limit_bytes > 0:
        sub.traffic_limit_bytes += extra_bytes
    
    panel_ok, panel_err = await _sync_sub_to_remote_panel(sub, db_session)
    await db_session.commit()

    if panel_ok:
        msg = f"✅ مقدار {extra_gb} گیگابایت اضافه و روی پنل سرور اعمال شد!"
    else:
        msg = f"⚠️ مقدار {extra_gb} گیگ در دیتابیس ثبت شد اما اعمال روی پنل با خطا مواجه شد:\n{panel_err}"

    await callback.answer(msg, show_alert=True)
    await view_admin_sub_detail(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_sub_add_days:"))
async def admin_sub_add_days(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    parts = callback.data.split(":")
    sub_id = uuid.UUID(parts[1])
    extra_days = int(parts[2])

    sub = await db_session.get(Subscription, sub_id)
    if not sub:
        return await callback.answer("اشتراک یافت نشد", show_alert=True)

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cur_exp = sub.expires_at
    if cur_exp and cur_exp.tzinfo is None:
        cur_exp = cur_exp.replace(tzinfo=timezone.utc)
    base = cur_exp if (cur_exp and cur_exp > now) else now
    sub.expires_at = base + timedelta(days=extra_days)
    if sub.status != SubscriptionStatus.ACTIVE:
        sub.status = SubscriptionStatus.ACTIVE

    panel_ok, panel_err = await _sync_sub_to_remote_panel(sub, db_session)
    await db_session.commit()

    if panel_ok:
        msg = f"✅ مدت {extra_days} روز به اعتبار اضافه و روی پنل اعمال شد!"
    else:
        msg = f"⚠️ تاریخ تمدید شد اما اعمال روی پنل با خطا مواجه شد:\n{panel_err}"

    await callback.answer(msg, show_alert=True)
    await view_admin_sub_detail(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_sub_toggle_status:"))
async def admin_sub_toggle_status(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    sub_id = uuid.UUID(callback.data.split(":")[1])
    sub = await db_session.get(Subscription, sub_id)
    if not sub:
        return await callback.answer("اشتراک یافت نشد", show_alert=True)

    if sub.status == SubscriptionStatus.ACTIVE:
        sub.status = SubscriptionStatus.DISABLED
        status_action = "قطع موقت"
    else:
        sub.status = SubscriptionStatus.ACTIVE
        status_action = "فعال‌سازی مجدد"

    panel_ok, panel_err = await _sync_sub_to_remote_panel(sub, db_session)
    await db_session.commit()

    if panel_ok:
        msg = f"✅ اشتراک با موفقیت {status_action} و روی پنل سرور همگام شد."
    else:
        msg = f"⚠️ وضعیت اشتراک در دیتابیس {status_action} شد اما همگام‌سازی پنل با خطا مواجه شد:\n{panel_err}"

    await callback.answer(msg, show_alert=True)
    await view_admin_sub_detail(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_sub_reset_sub:"))
async def admin_sub_reset_sub(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    sub_id = uuid.UUID(callback.data.split(":")[1])
    sub = await db_session.get(Subscription, sub_id)
    if not sub:
        return await callback.answer("اشتراک یافت نشد", show_alert=True)

    from app.core.security import generate_sub_id
    sub.subscription_token = generate_sub_id(16)

    panel_ok, panel_err = await _sync_sub_to_remote_panel(sub, db_session)
    await db_session.commit()

    if panel_ok:
        msg = "✅ لینک سابسکریپشن کاربر بازنشانی و روی پنل همگام شد!"
    else:
        msg = f"⚠️ توکن در دیتابیس تغییر یافت اما همگام‌سازی پنل با خطا مواجه شد:\n{panel_err}"

    await callback.answer(msg, show_alert=True)
    await view_admin_sub_detail(callback, db_user, db_session)
    await callback.answer("✅ لینک سابسکریپشن کاربر با موفقیت تغییر کرد و توکن جدید صادر شد!", show_alert=True)
    await view_admin_sub_detail(callback, db_user, db_session)


@router.callback_query(F.data.startswith("admin_sub_get_qr:"))
async def admin_sub_get_qr(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    sub_id = uuid.UUID(callback.data.split(":")[1])
    from app.modules.subscriptions.service import SubscriptionService
    sub_svc = SubscriptionService(db_session)
    conn = await sub_svc.get_connection_details(sub_id)

    qr_buf = generate_qr_buffer(conn["direct_link"])
    qr_file = BufferedInputFile(qr_buf.getvalue(), filename="sub_qrcode.png")
    caption = (
        f"📱 <b>کانفیگ و بارکد اتصال:</b>\n\n"
        f"🌍 لوکیشن: <b>{conn['flag']} {conn['location']}</b>\n"
        f"🔒 پروتکل: <b>{conn['protocol']}</b>\n\n"
        f"🔗 <b>کانفیگ مستقیم:</b>\n<code>{conn['direct_link']}</code>\n\n"
        f"📱 <b>لینک ساب:</b>\n<code>{conn['subscription_url']}</code>"
    )
    await callback.message.answer_photo(photo=qr_file, caption=caption, parse_mode="HTML")
    await callback.answer()


# ==========================================
# COUPON MANAGEMENT (CREATE / DELETE / LIST)
# ==========================================

@router.callback_query(F.data == "admin_manage_coupons")
async def list_admin_coupons(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    coupons_res = await db_session.execute(select(Coupon).order_by(Coupon.created_at.desc()))
    coupons = list(coupons_res.scalars().all())

    kb_rows = [
        [InlineKeyboardButton(text="➕ ساخت کد تخفیف جدید", callback_data="admin_add_coupon_start")]
    ]
    for c in coupons:
        val = f"{c.discount_percent}%" if c.discount_percent > 0 else f"{c.discount_amount_irr // 10:,} ت"
        st = "🟢" if c.is_active else "🔴"
        kb_rows.append([
            InlineKeyboardButton(text=f"{st} {c.code} ({val}) - {c.current_uses}/{c.max_uses}", callback_data="noop"),
            InlineKeyboardButton(text="🗑 حذف", callback_data=f"admin_del_coupon:{c.id}")
        ])
    kb_rows.append([InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")])

    text = (
        f"🎟 <b>مدیریت کدهای تخفیف ({len(coupons)} کد):</b>\n\n"
        "در این بخش می‌توانید کدهای تخفیف درصدی برای جشنواره‌ها و کاربران بسازید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("admin_del_coupon:"))
async def delete_coupon(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    c_id = uuid.UUID(callback.data.split(":")[1])
    c = await db_session.get(Coupon, c_id)
    if c:
        await db_session.delete(c)
        await db_session.commit()
        await callback.answer("✅ کد تخفیف حذف شد.", show_alert=True)
    await list_admin_coupons(callback, db_user, db_session)


@router.callback_query(F.data == "admin_add_coupon_start")
async def start_add_coupon(callback: CallbackQuery, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    await state.set_state(AdminState.coupon_entering_code)
    await callback.message.edit_text(
        "🎟 <b>مرحله ۱ از ۳:</b>\nلطفاً <b>کد تخفیف</b> مورد نظر را وارد فرمایید:\n(مثال: <code>NOWRUZ</code> یا <code>VIP30</code>)",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminState.coupon_entering_code)
async def process_coupon_code(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    code = message.text.strip().upper()
    await state.update_data(coupon_code=code)
    await state.set_state(AdminState.coupon_entering_percent)
    await message.answer(
        f"کد: <code>{code}</code> ثبت شد.\n\n"
        "📊 <b>مرحله ۲ از ۳:</b>\nلطفاً <b>درصد تخفیف</b> را به عدد وارد نمایید:\n(مثال: برای ۲۰ درصد تخفیف عدد <code>20</code> را بفرستید)",
        parse_mode="HTML"
    )


@router.message(AdminState.coupon_entering_percent)
async def process_coupon_percent(message: Message, state: FSMContext, db_user: User):
    if not is_admin(db_user):
        return
    try:
        pct = int(message.text.strip())
        if not (1 <= pct <= 100):
            raise ValueError()
    except ValueError:
        return await message.answer("⚠️ لطفاً یک عدد معتبر بین ۱ تا ۱۰۰ وارد نمایید:")

    await state.update_data(coupon_percent=pct)
    await state.set_state(AdminState.coupon_entering_max_uses)
    await message.answer(
        f"تخفیف: <b>{pct}%</b> ثبت شد.\n\n"
        "👥 <b>مرحله ۳ از ۳:</b>\n<b>سقف تعداد مجاز استفاده</b> از این کد را وارد نمایید:\n(مثال: <code>50</code> یا <code>100</code>)",
        parse_mode="HTML"
    )


@router.message(AdminState.coupon_entering_max_uses)
async def process_coupon_max_uses(message: Message, state: FSMContext, db_user: User, db_session: AsyncSession):
    if not is_admin(db_user):
        return
    try:
        max_u = int(message.text.strip())
        if max_u <= 0:
            raise ValueError()
    except ValueError:
        return await message.answer("⚠️ لطفاً یک عدد بزرگتر از ۰ وارد نمایید:")

    data = await state.get_data()
    code = data["coupon_code"]
    pct = data["coupon_percent"]
    await state.clear()

    existing = await db_session.execute(select(Coupon).where(Coupon.code == code))
    if existing.scalar_one_or_none():
        return await message.answer("❌ این کد تخفیف از قبل وجود دارد!")

    new_coupon = Coupon(
        id=uuid.uuid4(),
        code=code,
        discount_percent=pct,
        discount_amount_irr=0,
        max_uses=max_u,
        current_uses=0,
        is_active=True,
    )
    db_session.add(new_coupon)
    await db_session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟 مشاهده کدهای تخفیف", callback_data="admin_manage_coupons")],
        [InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")],
    ])
    await message.answer(
        f"✅ <b>کد تخفیف با موفقیت ایجاد شد!</b>\n\n"
        f"🎟 کد: <code>{code}</code>\n"
        f"📊 میزان تخفیف: <b>{pct}%</b>\n"
        f"👥 سقف استفاده: <b>{max_u} بار</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )


# ---------------------------------------------------------------------------
# SUPPORT TICKETS MANAGEMENT IN ADMIN DASHBOARD
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("admin_view_tickets:"))
async def admin_view_tickets(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """List tickets for administrator with category filtering and pagination."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    parts = callback.data.split(":")
    filter_type = parts[1] if len(parts) > 1 else "open"
    page = int(parts[2]) if len(parts) > 2 else 0

    query = select(SupportTicket).options(selectinload(SupportTicket.user), selectinload(SupportTicket.messages))
    if filter_type == "open":
        query = query.where(SupportTicket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_CUSTOMER]))
    elif filter_type == "closed":
        query = query.where(SupportTicket.status.in_([TicketStatus.CLOSED, TicketStatus.RESOLVED]))

    query = query.order_by(SupportTicket.updated_at.desc())
    res = await db_session.execute(query)
    all_tickets = res.scalars().all()

    total_tickets = len(all_tickets)
    page_size = 5
    total_pages = max(1, (total_tickets + page_size - 1) // page_size)
    page = min(max(0, page), total_pages - 1)
    paged_tickets = all_tickets[page * page_size : (page + 1) * page_size]

    filter_title = {
        "open": "🟢 تیکت‌های باز / در انتظار بررسی",
        "closed": "🔒 تیکت‌های بسته شده",
        "all": "📋 همه تیکت‌ها",
    }.get(filter_type, "تیکت‌ها")

    text = (
        f"📩 <b>مدیریت تیکت‌های پشتیبانی</b>\n\n"
        f"فیلتر فعال: <b>{filter_title}</b>\n"
        f"📊 تعداد کل: <b>{total_tickets} تیکت</b> | صفحه <b>{page + 1} از {total_pages}</b>\n\n"
    )

    if not paged_tickets:
        text += "<i>هیچ تیکتی در این دسته‌بندی وجود ندارد.</i>\n"

    kb_rows = []
    for t in paged_tickets:
        st_icon = "🟢" if t.status in [TicketStatus.OPEN, TicketStatus.IN_PROGRESS] else "🔒"
        sub_preview = t.subject[:25] + ("..." if len(t.subject) > 25 else "")
        btn_label = f"{st_icon} #{t.id.hex[:6]} | {sub_preview}"
        kb_rows.append([InlineKeyboardButton(text=btn_label, callback_data=f"adm_tk_dtl:{t.id.hex}")])

    # Filter selector buttons
    filter_row = []
    if filter_type != "open":
        filter_row.append(InlineKeyboardButton(text="🟢 تیکت‌های باز", callback_data="admin_view_tickets:open:0"))
    if filter_type != "closed":
        filter_row.append(InlineKeyboardButton(text="🔒 بسته‌شده", callback_data="admin_view_tickets:closed:0"))
    if filter_type != "all":
        filter_row.append(InlineKeyboardButton(text="📋 همه تیکت‌ها", callback_data="admin_view_tickets:all:0"))
    if filter_row:
        kb_rows.append(filter_row)

    # Pagination buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ صفحه قبلی", callback_data=f"admin_view_tickets:{filter_type}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="صفحه بعدی ▶️", callback_data=f"admin_view_tickets:{filter_type}:{page + 1}"))
    if nav_row:
        kb_rows.append(nav_row)

    kb_rows.append([InlineKeyboardButton(text="🔙 بازگشت به داشبورد", callback_data="admin_refresh_dashboard")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_tk_dtl:") | F.data.startswith("admin_ticket_detail:"))
async def admin_ticket_detail(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """View ticket conversation history and options to reply or close/reopen."""
    try:
        await callback.answer()
    except Exception:
        pass

    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)

    import html

    ticket_id_str = callback.data.split(":")[1]
    try:
        tid_uuid = uuid.UUID(hex=ticket_id_str) if len(ticket_id_str) == 32 else uuid.UUID(ticket_id_str)
    except Exception:
        return await callback.answer("شناسه تیکت نامعتبر است.", show_alert=True)

    res = await db_session.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.user), selectinload(SupportTicket.messages))
        .where(SupportTicket.id == tid_uuid)
    )
    ticket = res.scalar_one_or_none()
    if not ticket:
        return await callback.answer("تیکت یافت نشد.", show_alert=True)

    user = ticket.user
    customer_tg = user.telegram_id if user else 0
    raw_customer_name = f"{user.first_name} (@{user.username})" if (user and user.username) else (user.first_name if user else "ناشناس")
    customer_name = html.escape(raw_customer_name)
    subject_safe = html.escape(ticket.subject or "بدون موضوع")
    category_safe = html.escape(ticket.category or "عمومی")

    status_fa = {
        TicketStatus.OPEN: "🟢 باز / جدید",
        TicketStatus.IN_PROGRESS: "🟡 در حال بررسی",
        TicketStatus.WAITING_FOR_CUSTOMER: "⏳ منتظر پاسخ کاربر",
        TicketStatus.CLOSED: "🔒 بسته شده",
        TicketStatus.RESOLVED: "✅ حل شده",
    }.get(ticket.status, ticket.status)

    text = (
        f"🎫 <b>جزئیات تیکت شماره #{ticket.id.hex[:6]}</b>\n\n"
        f"👤 <b>کاربر:</b> {customer_name}\n"
        f"🆔 <b>شناسه عددی کاربر:</b> <code>{customer_tg}</code>\n"
        f"📌 <b>موضوع:</b> {subject_safe}\n"
        f"🏷 <b>دسته‌بندی:</b> {category_safe}\n"
        f"📊 <b>وضعیت:</b> {status_fa}\n"
        f"📅 <b>زمان ثبت:</b> {ticket.created_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"💬 <b>تاریخچه پیام‌ها ({len(ticket.messages)} پیام):</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    for msg in ticket.messages:
        sender_lbl = "👤 <b>کاربر:</b>" if msg.sender_type == "CUSTOMER" else "👑 <b>پشتیبانی/مدیریت:</b>"
        attach_note = " 📎 <i>[دارای پیوست]</i>" if msg.attachment_file_id else ""
        msg_time = msg.created_at.strftime("%H:%M")
        safe_msg_text = html.escape(msg.text or "")
        text += f"{sender_lbl} ({msg_time}){attach_note}\n{safe_msg_text}\n\n"

    text += "━━━━━━━━━━━━━━━━━━━\n<i>جهت پاسخ می‌توانید روی دکمه زیر کلیک کنید یا مستقیماً روی پیام تیکت Reply دهید.</i>"

    kb_rows = [
        [
            InlineKeyboardButton(text="✉️ ارسال پاسخ به این تیکت", callback_data=f"adm_rpl_tk:{ticket.id.hex}:{customer_tg}"),
        ]
    ]

    if ticket.status in [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_CUSTOMER]:
        kb_rows.append([InlineKeyboardButton(text="🔒 بستن این تیکت", callback_data=f"adm_cls_tk:{ticket.id.hex}")])
    else:
        kb_rows.append([InlineKeyboardButton(text="🔓 بازگشایی مجدد تیکت", callback_data=f"adm_ropn_tk:{ticket.id.hex}")])

    kb_rows.append([
        InlineKeyboardButton(text="🔙 لیست تیکت‌ها", callback_data="admin_view_tickets:open:0"),
        InlineKeyboardButton(text="🏠 پنل مدیریت", callback_data="admin_refresh_dashboard"),
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        if callback.message.photo or callback.message.document:
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e_edit:
        logger.warning(f"Could not edit message for ticket detail: {e_edit}. Sending new message.")
        try:
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
        except Exception as e_html:
            logger.error(f"Failed to send HTML ticket detail: {e_html}. Sending plain text.")
            import re
            plain_text = re.sub(r"<[^>]+>", "", text)
            await callback.message.answer(plain_text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm_ropn_tk:") | F.data.startswith("admin_reopen_ticket:"))
async def handle_admin_reopen_ticket(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Reopen a previously closed ticket."""
    if not is_admin(db_user):
        return await callback.answer("دسترسی غیرمجاز", show_alert=True)
    ticket_id_str = callback.data.split(":")[1]
    tid_uuid = uuid.UUID(hex=ticket_id_str) if len(ticket_id_str) == 32 else uuid.UUID(ticket_id_str)
    ticket = await db_session.get(SupportTicket, tid_uuid)
    if ticket:
        ticket.status = TicketStatus.OPEN
        await db_session.commit()
        await callback.answer("🔓 تیکت مجدداً بازگشایی شد.", show_alert=True)
        callback.data = f"adm_tk_dtl:{ticket.id.hex}"
        await admin_ticket_detail(callback, db_user, db_session)
    else:
        await callback.answer("تیکت یافت نشد.", show_alert=True)

