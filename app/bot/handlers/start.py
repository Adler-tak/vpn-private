"""Start and general informational handlers."""
import re
import logging
from datetime import datetime, timezone
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User
from app.database.models.subscription import Subscription
from app.modules.users.service import UserService
from app.modules.subscriptions.service import SubscriptionService
from app.bot.locales.fa import WELCOME_MSG, RULES_TEXT, TUTORIALS_TEXT, MAIN_MENU_BTNS
from app.modules.bot_texts.service import get_bot_text
from app.bot.keyboards.default import get_main_reply_keyboard, get_user_reply_keyboard, get_effective_webapp_url
from app.core.constants import UserRole
from app.core.config import settings
from app.bot.handlers.admin import generate_qr_buffer
from app.services.settings_service import get_effective_bot_username_sync

logger = logging.getLogger(__name__)

router = Router()


@router.message(CommandStart())
async def handle_start(
    message: Message,
    command: CommandObject,
    db_user: User,
    db_session: AsyncSession,
    bot: Bot = None,
    state: FSMContext = None,
    is_new_user: bool = False,
):
    """Handle /start with optional referral code or voucher redemption deep-link."""
    if state:
        await state.clear()

    # Record Audit Log and notify admins if new user or bot start
    try:
        from app.database.models.audit import AuditLog
        if is_new_user:
            audit_entry = AuditLog(
                actor_id=db_user.telegram_id,
                actor_role="CUSTOMER",
                action="NEW_USER_REGISTERED",
                target_type="User",
                target_id=str(db_user.id),
                details=f"ثبت‌نام و استارت کاربر جدید {db_user.first_name or ''} {db_user.last_name or ''} (@{db_user.username or 'ندارد'})",
            )
            db_session.add(audit_entry)
            await db_session.flush()

            # Broadcast Telegram Alert to Admins
            from app.bot.notifications import notify_all_admins
            full_name = f"{db_user.first_name or ''} {db_user.last_name or ''}".strip() or "کاربر تلگرام"
            uname = f"@{db_user.username}" if db_user.username else "ندارد"
            ref_txt = f"<code>{db_user.referred_by_id}</code>" if db_user.referred_by_id else "مستقیم (بدون معرف)"
            admin_msg = (
                f"👤 <b>عضو جدید ربات را استارت کرد!</b>\n\n"
                f"▫️ <b>نام:</b> {full_name}\n"
                f"▫️ <b>یوزرنیم:</b> {uname}\n"
                f"▫️ <b>شناسه عددی (Chat ID):</b> <code>{db_user.telegram_id}</code>\n"
                f"▫️ <b>معرف:</b> {ref_txt}\n"
                f"▫️ <b>تاریخ و زمان:</b> <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}</code>"
            )
            effective_bot = bot or message.bot
            if effective_bot:
                await notify_all_admins(bot=effective_bot, db_session=db_session, text=admin_msg)
        else:
            audit_entry = AuditLog(
                actor_id=db_user.telegram_id,
                actor_role="CUSTOMER",
                action="USER_STARTED_BOT",
                target_type="User",
                target_id=str(db_user.id),
                details=f"استارت مجدد ربات توسط {db_user.first_name or ''} (@{db_user.username or 'ندارد'})",
            )
            db_session.add(audit_entry)
            await db_session.flush()
    except Exception as e_start_alert:
        logger.error(f"Error handling start audit/alert for user {db_user.telegram_id}: {e_start_alert}")

    is_admin_user = (
        db_user.telegram_id in settings.ADMIN_TELEGRAM_IDS
        or db_user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.FINANCE, UserRole.SUPPORT)
    )
    is_reseller_user = (db_user.role == UserRole.RESELLER)
    kb = get_main_reply_keyboard(is_admin=is_admin_user, is_reseller=is_reseller_user)

    # 1. Check for voucher redemption deep-linking (e.g. start=v_VPN-XXXX-XXXX-XXXX)
    if command and command.args:
        arg_val = command.args.strip()
        if arg_val.startswith("gift_") or arg_val.startswith("gift-"):
            gift_code = arg_val[5:].strip()
            from app.services.gift_card_service import GiftCardService
            info = await GiftCardService.get_gift_info(gift_code, db_session)
            if not info.get("exists"):
                await message.answer("❌ کارت هدیه مورد نظر در سیستم یافت نشد یا نامعتبر است.", reply_markup=kb)
                return
            if info.get("is_used"):
                await message.answer("⚠️ این کارت هدیه قبلاً استفاده و فعال شده است.", reply_markup=kb)
                return

            import html as pyhtml
            sender_name = pyhtml.escape(info.get("sender_name") or "یک دوست عزیز")
            gift_note = pyhtml.escape(info.get("gift_note") or "")
            prod_name = pyhtml.escape(info.get("product_name") or "اشتراک پرسرعت")
            traffic_gb = info.get("traffic_gb") or 0
            days = info.get("duration_days") or 30

            gift_text = (
                f"🎁✨ <b>تبریک! شما یک کارت هدیه اشتراک VPN Private دریافت کردید!</b> ✨🎁\n\n"
                f"💌 <b>از طرف:</b> <code>{sender_name}</code>\n"
                f"📝 <b>پیام هدیه‌دهنده:</b>\n<i>«{gift_note}»</i>\n\n"
                f"📦 <b>مشخصات هدیه:</b> {prod_name} ({traffic_gb} گیگابایت - {days} روزه)\n\n"
                f"جهت باز کردن هدیه و دریافت آنی لینک اتصال و QR Code، روی دکمه زیر کلیک کنید: 👇"
            )
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            gift_inline_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🎁 باز کردن و فعال‌سازی آنی هدیه", callback_data=f"open_gift:{gift_code}")
            ]])
            await message.answer(gift_text, reply_markup=gift_inline_kb, parse_mode="HTML")
            return

        if arg_val.startswith("v_") or arg_val.startswith("v-"):
            voucher_code = arg_val[2:]
            await message.answer(
                "🎁 <b>کارت ووچر شناسایی شد!</b> در حال فعال‌سازی خودکار...",
                reply_markup=kb,
                parse_mode="HTML",
            )
            from app.bot.handlers.voucher import process_voucher_redemption
            await process_voucher_redemption(
                target_message=message,
                user=db_user,
                code=voucher_code,
                db_session=db_session,
                bot=bot,
            )
            return

    # 2. Check for referral code
    referrer_code = command.args if command and command.args else None
    if referrer_code:
        user_service = UserService(db_session)
        # Register or assign referral if user was just created
        await user_service.register_or_get_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            referrer_code=referrer_code,
        )

    from app.services.settings_service import get_effective_bot_username_sync
    from app.bot.keyboards.default import get_effective_webapp_url
    ch = getattr(settings, "CHANNEL_USERNAME", "ADVPN_Private").strip().lstrip("@")
    sup = getattr(settings, "SUPPORT_USERNAME", "VPNPrivateSupport").strip().lstrip("@")
    bot_user = get_effective_bot_username_sync().strip().lstrip("@")
    welcome_text = get_bot_text("start_welcome", WELCOME_MSG)
    welcome_text = re.sub(r"https://t\.me/(?:ADVPN_Private|[A-Za-z0-9_]*channel[A-Za-z0-9_]*)", f"https://t.me/{ch}", welcome_text)
    welcome_text = re.sub(r"@ADVPN_Private", f"@{ch}", welcome_text)
    welcome_text = re.sub(r"https://t\.me/(?:VPNPrivateSupport|VPNPrivateSupport)", f"https://t.me/{sup}", welcome_text)
    welcome_text = re.sub(r"@VPNPrivateSupport", f"@{sup}", welcome_text)
    welcome_text = re.sub(r"https://t\.me/(?:VPNPrivateSell_Bot|VPNPrivateBot)", f"https://t.me/{bot_user}", welcome_text)
    welcome_text = re.sub(r"@(VPNPrivateSell_Bot|VPNPrivateBot)", f"@{bot_user}", welcome_text)

    # Ensure user has the WebApp menu button configured
    if bot:
        webapp_url = get_effective_webapp_url()
        if webapp_url.startswith("https://"):
            try:
                from aiogram.types import MenuButtonWebApp, WebAppInfo
                await bot.set_chat_menu_button(
                    chat_id=message.chat.id,
                    menu_button=MenuButtonWebApp(text="App", web_app=WebAppInfo(url=webapp_url)),
                )
            except Exception as e:
                logger.debug(f"Could not set chat menu button in handle_start: {e}")

    await message.answer(
        welcome_text,
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.message(Command("webapp"))
@router.message(Command("app"))
@router.message(Command("miniapp"))
@router.message(F.text.func(lambda t: bool(t and any(k in t.lower() for k in [
    "app", "مینی‌اپ", "مینی اپ", "ورود به مینی‌اپ", "ورود به مینی اپ",
    "اپلیکیشن مدیریت سرویس", "miniapp", "webapp"
]))))
async def handle_webapp_command(message: Message, db_user: User, bot: Bot = None):
    """Send direct Telegram WebApp launching button and ensure chat menu button is active."""
    from app.bot.keyboards.default import get_effective_webapp_url
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp

    webapp_url = get_effective_webapp_url()
    if not webapp_url.startswith("https://") and not ("localhost" in webapp_url or "127.0.0.1" in webapp_url):
        return await message.answer(
            "⚠️ آدرس مینی‌اپ هنوز با پروتکل امن HTTPS در سیستم تنظیم نشده است.\nلطفاً با مدیر سیستم تماس بگیرید."
        )

    if bot and webapp_url.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=MenuButtonWebApp(text="App", web_app=WebAppInfo(url=webapp_url)),
            )
        except Exception as e:
            logger.debug(f"Could not update chat menu button: {e}")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="App", web_app=WebAppInfo(url=webapp_url))
    ]])
    await message.answer(
        "🛍 <b>اپلیکیشن مدیریت سرویس VPN Private (Mini App)</b> 🌐✨\n\n"
        "جهت مشاهده وضعیت حجم مصرفی، تمدید اشتراک، چرخاندن گردونه شانس و خرید کارت هدیه، روی دکمه زیر کلیک کنید: 👇",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "check_channel_membership")
async def handle_check_channel_membership(callback: CallbackQuery, db_user: User, bot: Bot):
    """Verify if user has joined the channel and unlock the bot."""
    channel = getattr(settings, "CHANNEL_USERNAME", "").strip().lstrip("@")
    if not channel:
        await callback.answer("کانالی تنظیم نشده است.", show_alert=True)
        return

    is_member = False
    try:
        member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=callback.from_user.id)
        status = getattr(member, "status", "")
        is_member = status in ("creator", "administrator", "member", "restricted")
    except Exception as e:
        logger.warning(f"Error checking membership for {callback.from_user.id} in @{channel}: {e}")
        is_member = False

    if is_member:
        await callback.answer("✅ عضویت شما تایید شد! به ربات خوش آمدید.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        is_admin_user = (
            db_user.telegram_id in settings.ADMIN_TELEGRAM_IDS
            or db_user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.FINANCE, UserRole.SUPPORT)
        )
        is_reseller_user = (db_user.role == UserRole.RESELLER)
        kb = get_main_reply_keyboard(is_admin=is_admin_user, is_reseller=is_reseller_user)
        ch = getattr(settings, "CHANNEL_USERNAME", "ADVPN_Private").strip().lstrip("@")
        sup = getattr(settings, "SUPPORT_USERNAME", "VPNPrivateSupport").strip().lstrip("@")
        bot_user = get_effective_bot_username_sync().strip().lstrip("@")
        welcome_text = get_bot_text("start_welcome", WELCOME_MSG)
        welcome_text = re.sub(r"https://t\.me/(?:ADVPN_Private|[A-Za-z0-9_]*channel[A-Za-z0-9_]*)", f"https://t.me/{ch}", welcome_text)
        welcome_text = re.sub(r"@ADVPN_Private", f"@{ch}", welcome_text)
        welcome_text = re.sub(r"https://t\.me/(?:VPNPrivateSupport|VPNPrivateSupport)", f"https://t.me/{sup}", welcome_text)
        welcome_text = re.sub(r"@VPNPrivateSupport", f"@{sup}", welcome_text)
        welcome_text = re.sub(r"https://t\.me/(?:VPNPrivateSell_Bot|VPNPrivateBot)", f"https://t.me/{bot_user}", welcome_text)
        welcome_text = re.sub(r"@(VPNPrivateSell_Bot|VPNPrivateBot)", f"@{bot_user}", welcome_text)

        if bot:
            webapp_url = get_effective_webapp_url()
            if webapp_url.startswith("https://"):
                try:
                    from aiogram.types import MenuButtonWebApp, WebAppInfo
                    await bot.set_chat_menu_button(
                        chat_id=callback.from_user.id,
                        menu_button=MenuButtonWebApp(text="App", web_app=WebAppInfo(url=webapp_url)),
                    )
                except Exception:
                    pass

        await bot.send_message(
            chat_id=callback.from_user.id,
            text=welcome_text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    else:
        await callback.answer(
            "⚠️ شما هنوز در کانال عضو نشده‌اید!\n\n"
            "لطفاً ابتدا روی دکمه «📢 عضویت در کانال رسمی» کلیک کرده و عضو شوید، سپس مجدداً این دکمه را لمس فرمایید.",
            show_alert=True,
        )


@router.message(F.text == MAIN_MENU_BTNS["rules"])
async def handle_rules(message: Message, db_user: User):
    rules_text = get_bot_text("rules_terms", RULES_TEXT)
    kb = get_user_reply_keyboard(db_user)
    await message.answer(rules_text, reply_markup=kb, parse_mode="HTML")


@router.message(F.text == MAIN_MENU_BTNS["tutorial"])
async def handle_tutorials(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 دانلود v2rayNG (اندروید)", url="https://github.com/2dust/v2rayNG/releases"),
            InlineKeyboardButton(text="📱 دانلود Happ (اندروید)", url="https://github.com/happ-proxy/happ-android/releases"),
        ],
        [
            InlineKeyboardButton(text="🍏 برنامه Streisand (آیفون)", url="https://apps.apple.com/app/streisand/id6450534064"),
            InlineKeyboardButton(text="🍏 برنامه V2Box (آیفون)", url="https://apps.apple.com/app/v2box-v2ray-client/id6446814042"),
        ],
        [
            InlineKeyboardButton(text="💻 برنامه v2rayN (ویندوز)", url="https://github.com/2dust/v2rayN/releases"),
        ],
    ])
    await message.answer(TUTORIALS_TEXT, reply_markup=kb, parse_mode="HTML")


@router.message(F.text == MAIN_MENU_BTNS["trial"])
async def handle_free_trial(message: Message, db_user: User, db_session: AsyncSession):
    """Deliver a one-time free trial config to customer."""
    kb = get_user_reply_keyboard(db_user)
    if not getattr(settings, "TRIAL_ENABLED", True):
        return await message.answer(
            "⚠️ <b>دریافت اکانت تست موقتاً توسط مدیریت غیرفعال شده است.</b>\n"
            "جهت مشاهده و خرید اشتراک‌های پرسرعت، از گزینه <b>🛒 خرید سرویس</b> استفاده فرمایید.",
            reply_markup=kb,
            parse_mode="HTML",
        )

    # Lock user row to prevent race conditions during concurrent trial requests
    await db_session.execute(
        select(User.id).where(User.id == db_user.id).with_for_update()
    )

    # Check if user already reached their allowed trial limit
    trial_count_res = await db_session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.user_id == db_user.id,
            Subscription.client_email.like("trial_%"),
        )
    )
    user_trial_count = trial_count_res.scalar() or 0
    max_trials = getattr(settings, "TRIAL_MAX_COUNT", 1)

    if user_trial_count >= max_trials:
        return await message.answer(
            f"⚠️ <b>شما قبلاً سقف مجاز اکانت تست رایگان خود ({max_trials} مرتبه) را استفاده کرده‌اید!</b>\n\n"
            "برای دریافت اشتراک پرسرعت و پایدار، لطفاً از منوی اصلی گزینه <b>🛒 خرید سرویس</b> را انتخاب نمایید.",
            reply_markup=kb,
            parse_mode="HTML",
        )

    # Provision trial
    sub_service = SubscriptionService(db_session)
    wait_msg = await message.answer("⏳ در حال ساخت اکانت تست رایگان برای شما...")
    try:
        sub = await sub_service.provision_trial_subscription(db_user)
        conn = await sub_service.get_connection_details(sub.id)
        
        try:
            from app.database.models.audit import AuditLog
            audit_trial = AuditLog(
                actor_id=db_user.telegram_id,
                actor_role="CUSTOMER",
                action="TRIAL_ACTIVATED",
                target_type="Subscription",
                target_id=str(sub.id),
                details=f"فعال‌سازی اکانت تست رایگان توسط {db_user.first_name or ''} (@{db_user.username or 'ندارد'})",
            )
            db_session.add(audit_trial)
        except Exception as e_at:
            logger.warning(f"Failed to record trial audit: {e_at}")

        await db_session.commit()

        qr_buf = generate_qr_buffer(conn["subscription_url"])
        qr_file = BufferedInputFile(qr_buf.getvalue(), filename="trial_qrcode.png")

        traffic_gb = settings.TRIAL_TRAFFIC_MB / 1024
        traffic_str = f"{traffic_gb:g} گیگابایت"
        duration_days = getattr(settings, "TRIAL_DURATION_DAYS", 1)

        trial_footer = get_bot_text(
            "free_trial_guide",
            "💡 <i>لینک فوق را کپی کرده یا بارکد QR را درون اپلیکیشن Happ، Streisand یا v2rayNG اسکن فرمایید.</i>",
        )
        trial_text = (
            f"🎁 <b>اکانت تست رایگان شما با موفقیت فعال شد!</b> 🌐✨\n\n"
            f"🌍 لوکیشن: <b>{conn['flag']} {conn['location']}</b>\n"
            f"📦 حجم هدیه: <b>{traffic_str}</b> ({settings.TRIAL_TRAFFIC_MB} MB)\n"
            f"⏰ اعتبار: <b>{duration_days} روز</b> ({settings.TRIAL_DURATION_HOURS} ساعت)\n"
            f"🔒 پروتکل: <b>{conn['protocol']}</b>\n\n"
            f"🔗 <b>لینک هوشمند سابسکریپشن:</b>\n"
            f"<code>{conn['subscription_url']}</code>\n\n"
            f"📱 <b>نرم‌افزار پیشنهادی (Happ Proxy):</b>\n"
            f"🍏 <a href=\"https://apps.apple.com/app/happ-proxy-utility/id6504287215\">دانلود نسخه iOS (آیفون)</a>\n"
            f"🤖 <a href=\"https://play.google.com/store/apps/details?id=com.happproxy\">دانلود نسخه اندروید</a>\n\n"
            f"{trial_footer}"
        )
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await message.answer_photo(photo=qr_file, caption=trial_text, reply_markup=kb, parse_mode="HTML")

    except Exception as exc:
        await wait_msg.edit_text(f"❌ خطا در فعال‌سازی اکانت تست: {exc}")

