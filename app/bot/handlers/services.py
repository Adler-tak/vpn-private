"""Customer subscription management, real-time traffic view, and renewal handlers."""
import io
import uuid
import html
import secrets
import qrcode
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.user import User, Wallet
from app.database.models.subscription import Subscription
from app.database.models.panel import Server
from app.database.models.product import Product
from app.database.models.payment import Payment, PaymentReceipt, BankCard, get_active_bank_card_or_fallback
from app.modules.subscriptions.service import SubscriptionService
from app.modules.wallets.service import WalletService
from app.bot.locales.fa import MAIN_MENU_BTNS
from app.modules.bot_texts.service import get_bot_text
from app.bot.keyboards.inline import get_subscription_actions_keyboard, get_admin_payment_approval_keyboard
from app.bot.states.customer import SubRenewState
from app.bot.notifications import notify_all_admins
from app.core.constants import SubscriptionStatus, UserRole, PaymentMethod, PaymentStatus
from app.core.config import settings
from app.services.reseller_service import ResellerService

router = Router()
logger = logging.getLogger(__name__)


def generate_qr_buffer(content: str) -> io.BytesIO:
    """Generate a PNG image buffer for QR code, safely handling oversized payloads."""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2,
        )
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
    except Exception as exc:
        logger.warning(f"QR code payload too large ({len(content)} chars), falling back to safe version 40 content: {exc}")
        qr = qrcode.QRCode(
            version=40,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=2,
        )
        safe_content = content[:2500]
        qr.add_data(safe_content)
        qr.make(fit=False)
        img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@router.message(F.text.in_([MAIN_MENU_BTNS["my_services"], MAIN_MENU_BTNS["usage"], MAIN_MENU_BTNS["renew"]]))
async def list_user_services(message: Message, db_user: User, db_session: AsyncSession):
    """List customer subscriptions with quick stats and action buttons (active and expired)."""
    subs_res = await db_session.execute(
        select(Subscription).where(
            and_(
                Subscription.user_id == db_user.id,
                Subscription.status.in_([
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.EXPIRED,
                    SubscriptionStatus.SUSPENDED,
                    SubscriptionStatus.DISABLED,
                ]),
            )
        ).order_by(Subscription.created_at.desc())
    )
    subscriptions = list(subs_res.scalars().all())

    if not subscriptions:
        await message.answer(
            "📭 شما در حال حاضر هیچ سرویسی ندارید.\nجهت تهیه اشتراک، از منوی اصلی دکمه **🛒 خرید سرویس** را انتخاب فرمایید."
        )
        return

    services_guide = get_bot_text(
        "services_guide",
        "📦 <b>سرویس‌های شما:</b>\n\nدر این بخش می‌توانید مشخصات، حجم مصرفی و وضعیت اتصال اشتراک‌های خود را مشاهده و مدیریت فرمایید.",
    )
    if services_guide:
        await message.answer(services_guide, parse_mode="HTML")

    sub_service = SubscriptionService(db_session)

    for sub in subscriptions:
        server = await db_session.get(Server, sub.server_id)
        expiry_str = sub.expires_at.strftime("%Y-%m-%d %H:%M") if sub.expires_at else "نامحدود"
        safe_email = html.escape(sub.client_email or "")
        loc_name = html.escape(server.location_name if server else "نامشخص")
        safe_flag = html.escape(server.flag_emoji if server else "🌐")
        safe_proto = html.escape((sub.protocol or "").upper())

        status_badge = "🟢 <b>فعال</b>"
        if sub.status == SubscriptionStatus.EXPIRED:
            status_badge = "🔴 <b>منقضی شده (جهت اتصال مجدد روی دکمه تمدید کلیک کنید)</b>"
        elif sub.status in (SubscriptionStatus.SUSPENDED, SubscriptionStatus.DISABLED):
            status_badge = "🟡 <b>قطع موقت / اتمام حجم</b>"

        details = (
            f"📦 <b>سرویس:</b> <code>{safe_email}</code>\n\n"
            f"🌍 سرور: {safe_flag} <b>{loc_name}</b>\n"
            f"📡 پروتکل: <b>{safe_proto}</b>\n"
            f"📊 مصرف ترافیک: <b>{sub.traffic_used_gb} / {sub.traffic_limit_gb} گیگابایت</b>\n"
            f"⏳ تاریخ انقضا: <b>{expiry_str}</b>\n"
            f"📊 وضعیت: {status_badge}\n"
        )
        await message.answer(
            details,
            reply_markup=get_subscription_actions_keyboard(str(sub.id), protocol=sub.protocol, auto_renew=bool(getattr(sub, "auto_renew", False))),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("sub_qr"))
async def send_sub_qr(callback: CallbackQuery, db_session: AsyncSession, db_user: Optional[User] = None):
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    try:
        sub_id = uuid.UUID(parts[1].strip())
    except ValueError:
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    sub = await db_session.get(Subscription, sub_id)
    if not sub:
        return await callback.answer("سرویس یافت نشد یا غیرفعال شده است.", show_alert=True)
    if db_user and sub.user_id != db_user.id and getattr(db_user, "role", None) != UserRole.ADMIN:
        return await callback.answer("دسترسی به این سرویس غیرمجاز است.", show_alert=True)

    sub_service = SubscriptionService(db_session)
    try:
        conn = await sub_service.get_connection_details(sub_id)
    except Exception as e:
        logger.error(f"Failed to fetch connection details for {sub_id}: {e}")
        return await callback.answer("خطا در بارگذاری مشخصات اشتراک.", show_alert=True)

    proto = conn.get("protocol", "").lower()
    if proto in ("wireguard", "amneziawg", "wg", "awg"):
        qr_content = conn.get("raw_config") or conn.get("direct_link") or conn.get("subscription_url", "")
    else:
        qr_content = conn.get("subscription_url", "")

    try:
        qr_buf = generate_qr_buffer(qr_content)
        qr_file = BufferedInputFile(qr_buf.getvalue(), filename="vpn_qr.png")
    except Exception as e:
        logger.error(f"Failed to generate QR buffer: {e}")
        return await callback.answer("خطا در ساخت تصویر بارکد.", show_alert=True)

    raw_link = conn.get("direct_link") or conn.get("subscription_url") or ""
    safe_link = html.escape(raw_link)
    safe_sub_url = html.escape(conn.get("subscription_url") or "")

    if proto in ("wireguard", "wg"):
        caption = (
            f"📱 <b>بارکد و اطلاعات اتصال WireGuard:</b>\n\n"
            f"🔗 <b>لینک مستقیم کانفیگ:</b>\n<code>{safe_link}</code>\n\n"
            f"📱 <b>نرم‌افزار رسمی WireGuard:</b>\n"
            f"🍏 <a href=\"https://apps.apple.com/app/wireguard/id1441195209\">دانلود نسخه iOS (آیفون)</a>\n"
            f"🤖 <a href=\"https://play.google.com/store/apps/details?id=com.wireguard.android\">دانلود نسخه اندروید</a>\n"
            f"💻 <a href=\"https://www.wireguard.com/install/\">دانلود نسخه ویندوز</a>\n\n"
            f"💡 <b>راهنمای اتصال:</b> در اپلیکیشن WireGuard دکمه <b>+</b> را لمس نموده و گزینه <b>Scan QR Code</b> را انتخاب و تصویر فوق را اسکن فرمایید. همچنین می‌توانید فایل کانفیگ (.conf) را از منو دریافت و ایمپورت نمایید."
        )
    elif proto in ("amneziawg", "awg"):
        caption = (
            f"📱 <b>بارکد و اطلاعات اتصال AmneziaWG (ضدفیلتر):</b>\n\n"
            f"🔗 <b>لینک مستقیم کانفیگ:</b>\n<code>{safe_link}</code>\n\n"
            f"📱 <b>نرم‌افزار رسمی AmneziaWG:</b>\n"
            f"🍏 <a href=\"https://apps.apple.com/app/amneziawg/id6478942364\">دانلود نسخه iOS (آیفون)</a>\n"
            f"🤖 <a href=\"https://play.google.com/store/apps/details?id=org.amnezia.awg\">دانلود نسخه اندروید</a>\n"
            f"💻 <a href=\"https://github.com/amnezia-vpn/amneziawg-windows-client/releases\">دانلود نسخه ویندوز</a>\n\n"
            f"💡 <b>راهنمای اتصال:</b> در اپلیکیشن AmneziaWG دکمه <b>+</b> را زده و <b>Scan QR Code</b> را انتخاب و بارکد فوق را اسکن نمایید، یا فایل کانفیگ (.conf) را دریافت و وارد (Import) کنید."
        )
    elif proto in ("openvpn", "ovpn"):
        caption = (
            f"📱 <b>بارکد و اطلاعات اتصال OpenVPN:</b>\n\n"
            f"🔗 <b>لینک اتصال:</b>\n<code>{safe_link}</code>\n\n"
            f"📱 <b>نرم‌افزار رسمی OpenVPN Connect:</b>\n"
            f"🍏 <a href=\"https://apps.apple.com/app/openvpn-connect/id590379981\">دانلود نسخه iOS (آیفون)</a>\n"
            f"🤖 <a href=\"https://play.google.com/store/apps/details?id=net.openvpn.openvpn\">دانلود نسخه اندروید</a>\n\n"
            f"💡 <b>راهنمای اتصال:</b> از منوی مدیریت سرویس دکمه «دریافت فایل کانفیگ» را انتخاب کرده و فایل دانلود شده را در OpenVPN Connect وارد (Import Profile) فرمایید."
        )
    else:
        caption = (
            f"📱 <b>بارکد و لینک اتصال هوشمند:</b>\n\n"
            f"🔗 <b>لینک اشتراک:</b>\n<code>{safe_sub_url}</code>\n\n"
            f"📱 <b>نرم‌افزار پیشنهادی (Happ Proxy):</b>\n"
            f"🍏 <a href=\"https://apps.apple.com/app/happ-proxy-utility/id6504287215\">دانلود نسخه iOS (آیفون)</a>\n"
            f"🤖 <a href=\"https://play.google.com/store/apps/details?id=com.happproxy\">دانلود نسخه اندروید</a>\n\n"
            f"💡 <i>کافیست لینک اشتراک بالا را در اپلیکیشن وارد کرده یا بارکد QR را اسکن فرمایید.</i>"
        )
    if callback.message:
        await callback.message.answer_photo(photo=qr_file, caption=caption, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("sub_conf"))
async def send_sub_conf(callback: CallbackQuery, db_session: AsyncSession, db_user: Optional[User] = None):
    """Send client configuration file (.conf or .ovpn) for WireGuard, AWG, or OpenVPN."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    sub_id_str = parts[1].strip()
    try:
        sub_id = uuid.UUID(sub_id_str)
    except ValueError:
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    sub = await db_session.get(Subscription, sub_id)
    if not sub:
        return await callback.answer("سرویس یافت نشد یا غیرفعال شده است.", show_alert=True)
    if db_user and sub.user_id != db_user.id and getattr(db_user, "role", None) != UserRole.ADMIN:
        return await callback.answer("دسترسی به این سرویس غیرمجاز است.", show_alert=True)

    sub_service = SubscriptionService(db_session)
    try:
        conn = await sub_service.get_connection_details(sub_id)
    except Exception as e:
        logger.error(f"Failed to fetch connection details for {sub_id}: {e}")
        return await callback.answer("خطا در بارگذاری مشخصات اشتراک.", show_alert=True)

    raw_config = conn.get("raw_config")
    proto = conn.get("protocol", "").lower()
    if not raw_config:
        raw_config = conn.get("direct_link", "")

    if proto in ("openvpn", "ovpn"):
        filename = f"vpn_openvpn_{sub_id_str[:8]}.ovpn"
        guide_text = (
            "📥 <b>فایل کانفیگ اختصاصی OpenVPN:</b>\n\n"
            "فایل پیوست شده را دانلود کرده و در نرم‌افزار <b>OpenVPN Connect</b> وارد (Import) فرمایید.\n\n"
            "📱 <b>نرم‌افزار OpenVPN Connect:</b>\n"
            "🍏 <a href=\"https://apps.apple.com/app/openvpn-connect/id590379981\">نسخه iOS (آیفون)</a>\n"
            "🤖 <a href=\"https://play.google.com/store/apps/details?id=net.openvpn.openvpn\">نسخه اندروید</a>\n"
            "💻 <a href=\"https://openvpn.net/client-connect-vpn-for-windows/\">نسخه ویندوز</a>\n\n"
            "💡 <i>در نرم‌افزار گزینه Import Profile -> File را انتخاب کرده و این فایل را اضافه نمایید.</i>"
        )
    elif proto in ("amneziawg", "awg"):
        filename = f"vpn_amneziawg_{sub_id_str[:8]}.conf"
        guide_text = (
            "📥 <b>فایل کانفیگ اختصاصی AmneziaWG (ضدفیلتر):</b>\n\n"
            "فایل پیوست شده را ذخیره نموده و در نرم‌افزار <b>AmneziaWG</b> وارد (Import) فرمایید.\n\n"
            "📱 <b>نرم‌افزار AmneziaWG:</b>\n"
            "🍏 <a href=\"https://apps.apple.com/app/amneziawg/id6478942364\">نسخه iOS (آیفون)</a>\n"
            "🤖 <a href=\"https://play.google.com/store/apps/details?id=org.amnezia.awg\">نسخه اندروید</a>\n"
            "💻 <a href=\"https://github.com/amnezia-vpn/amneziawg-windows-client/releases\">نسخه ویندوز</a>\n\n"
            "💡 <i>در نرم‌افزار AmneziaWG دکمه Add Tunnel یا (+) را زده و Create from file or archive را انتخاب نمایید.</i>"
        )
    else:
        filename = f"vpn_wireguard_{sub_id_str[:8]}.conf"
        guide_text = (
            "📥 <b>فایل کانفیگ اختصاصی WireGuard:</b>\n\n"
            "فایل پیوست شده را ذخیره نموده و در نرم‌افزار <b>WireGuard</b> وارد (Import) فرمایید.\n\n"
            "📱 <b>نرم‌افزار WireGuard:</b>\n"
            "🍏 <a href=\"https://apps.apple.com/app/wireguard/id1441195209\">نسخه iOS (آیفون)</a>\n"
            "🤖 <a href=\"https://play.google.com/store/apps/details?id=com.wireguard.android\">نسخه اندروید</a>\n"
            "💻 <a href=\"https://www.wireguard.com/install/\">نسخه ویندوز</a>\n\n"
            "💡 <i>در نرم‌افزار WireGuard دکمه Add Tunnel یا (+) را زده و Create from file or archive را انتخاب نمایید.</i>"
        )

    file_bytes = (raw_config or "").encode("utf-8")
    conf_file = BufferedInputFile(file_bytes, filename=filename)

    if callback.message:
        await callback.message.answer_document(
            document=conf_file,
            caption=guide_text,
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("sub_grace:"))
async def handle_sub_grace(callback: CallbackQuery, db_session: AsyncSession, db_user: Optional[User] = None):
    """Handle 24h Emergency Grace Period activation from bot."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    sub_id_str = parts[1].strip()
    try:
        sub_id = uuid.UUID(sub_id_str)
    except ValueError:
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    if not db_user:
        return await callback.answer("کاربر یافت نشد.", show_alert=True)

    from app.services.emergency_grace_service import EmergencyGraceService
    result = await EmergencyGraceService.activate_emergency_grace(
        subscription_id=sub_id,
        user_id=db_user.id,
        session=db_session,
    )
    if not result.get("success"):
        return await callback.answer(result.get("message", "امکان فعال‌سازی وجود ندارد."), show_alert=True)

    expires_str = result.get('expires_at', '')[:16].replace('T', ' ')
    success_msg = (
        "🆘 <b>اتصال اضطراری ۲۴ ساعته فعال شد!</b> 🌐✨\n\n"
        "⚡️ <b>۱ گیگابایت ترافیک امانی</b> (به ارزش ۳,۰۰۰ تومان) به مدت <b>۲۴ ساعت</b> به اشتراک شما اضافه گردید.\n"
        f"⏳ <b>معتبر تا:</b> <code>{expires_str}</code>\n\n"
        "💡 <i>اکنون می‌توانید مجدداً به اینترنت متصل شوید و در فرصت مناسب پلن اصلی خود را تمدید فرمایید.</i>"
    )
    await callback.message.answer(success_msg, parse_mode="HTML")
    await callback.answer("اتصال اضطراری با موفقیت فعال شد ✅", show_alert=False)


@router.callback_query(F.data.startswith("sub_autorenew:"))
async def handle_sub_autorenew_toggle(callback: CallbackQuery, db_session: AsyncSession, db_user: Optional[User] = None):
    """Toggle auto-renew for a subscription from the Telegram bot."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    sub_id_str = parts[1].strip()
    try:
        sub_id = uuid.UUID(sub_id_str)
    except ValueError:
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    if not db_user:
        return await callback.answer("کاربر یافت نشد.", show_alert=True)

    from app.services.auto_renew_service import AutoRenewService
    res = await AutoRenewService.toggle_auto_renew(
        subscription_id=sub_id,
        user_id=db_user.id,
        session=db_session,
    )
    if not res.get("success"):
        return await callback.answer(res.get("message", "خطا در تغییر وضعیت."), show_alert=True)

    sub = await db_session.get(Subscription, sub_id)
    if sub:
        new_kb = get_subscription_actions_keyboard(
            str(sub.id),
            protocol=sub.protocol,
            auto_renew=bool(sub.auto_renew),
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=new_kb)
        except Exception:
            pass

    status_icon = "فعال شد ✅" if res.get("auto_renew") else "غیرفعال شد ❌"
    await callback.answer(f"تمدید خودکار از کیف پول {status_icon}", show_alert=True)


async def _resolve_sub_and_prod(
    sub_raw: str,
    prod_raw: str,
    db_user: User,
    db_session: AsyncSession,
) -> tuple[Optional[Subscription], Optional[Product]]:
    sub = None
    product = None
    sub_clean = sub_raw.strip().replace("-", "").lower()
    prod_clean = prod_raw.strip().replace("-", "").lower()

    if len(sub_clean) == 32:
        try:
            sub = await db_session.get(Subscription, uuid.UUID(sub_clean))
        except Exception:
            sub = None
    if not sub:
        subs_res = await db_session.execute(
            select(Subscription).where(Subscription.user_id == db_user.id)
        )
        for s in subs_res.scalars().all():
            if s.id.hex.startswith(sub_clean) or s.id.hex.lower() == sub_clean:
                sub = s
                break

    if len(prod_clean) == 32:
        try:
            product = await db_session.get(Product, uuid.UUID(prod_clean))
        except Exception:
            product = None
    if not product:
        prods_res = await db_session.execute(select(Product))
        for p in prods_res.scalars().all():
            if p.id.hex.startswith(prod_clean) or p.id.hex.lower() == prod_clean:
                product = p
                break

    return sub, product


@router.callback_query(F.data.startswith("sub_renew:") | F.data.startswith("sub_upgrade:"))
async def handle_sub_renew_start(
    callback: CallbackQuery,
    db_user: User,
    db_session: AsyncSession,
):
    """User initiates subscription renewal or plan upgrade; list available packages with compact callbacks."""
    is_upgrade = callback.data.startswith("sub_upgrade:")
    header_title = "⚡ <b>تغییر و ارتقای اشتراک:</b>" if is_upgrade else "🔄 <b>تمدید اشتراک اختصاصی:</b>"
    guide_note = (
        "<i>(با انتخاب پلن جدید، حجم و روزهای بسته به سرویس شما افزوده شده و لینک و کانفیگ شما بدون تغییر فعال می‌ماند.)</i>"
        if is_upgrade else
        "<i>(با تمدید، حجم جدید به سرویس شما اضافه و تاریخ انقضا تمدید می‌گردد. لینک و کانفیگ شما بدون تغییر باقی می‌ماند.)</i>"
    )

    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)
    
    sub_raw = parts[1].strip()
    sub = None
    sub_clean = sub_raw.replace("-", "").lower()
    if len(sub_clean) == 32:
        try:
            sub = await db_session.get(Subscription, uuid.UUID(sub_clean))
        except Exception:
            pass
    if not sub:
        subs_res = await db_session.execute(select(Subscription).where(Subscription.user_id == db_user.id))
        for s in subs_res.scalars().all():
            if s.id.hex.startswith(sub_clean):
                sub = s
                break

    if not sub or sub.user_id != db_user.id:
        return await callback.answer("سرویس یافت نشد یا دسترسی غیرمجاز است.", show_alert=True)

    products_res = await db_session.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.sort_order.asc())
    )
    products = list(products_res.scalars().all())
    if not products:
        return await callback.answer("در حال حاضر هیچ پلنی برای تمدید یا ارتقا تعریف نشده است.", show_alert=True)

    keyboard_buttons = []
    reseller_pct = await ResellerService.get_reseller_discount_percent(db_user.id, db_session) if db_user.role == UserRole.RESELLER else 0
    from app.services.flash_sale_service import FlashSaleService
    flash_info = FlashSaleService.get_flash_sale_info()
    is_flash = flash_info.get("is_active", False)
    flash_pct = flash_info.get("discount_percent", 0)

    for prod in products:
        price = prod.price_toman
        badge = ""
        if db_user.role == UserRole.RESELLER:
            discount = (price * reseller_pct) // 100
            price = max(0, price - discount)
        elif is_flash and flash_pct > 0:
            price = FlashSaleService.calculate_discounted_price(price)
            badge = f" [⚡️ {flash_pct}٪ تخفیف صاعقه‌ای]"
        
        # Compact callback under 30 bytes: rnp:{sub_hex12}:{prod_hex12}
        compact_cb = f"rnp:{sub.id.hex[:12]}:{prod.id.hex[:12]}"
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"📦 {prod.name} ({prod.traffic_gb}GB - {prod.duration_days} روز) | {price:,} تومان{badge}",
                callback_data=compact_cb,
            )
        ])
    keyboard_buttons.append([InlineKeyboardButton(text="🔙 انصراف و بازگشت", callback_data="sub_renew_cancel")])

    safe_email = html.escape(sub.client_email or "")
    flash_banner = f"⚡️ <b>{flash_info.get('title', 'تخفیف ویژه صاعقه‌ای')} ({flash_pct}٪ تخفیف اعمال شد)</b>\n\n" if (is_flash and flash_pct > 0 and db_user.role != UserRole.RESELLER) else ""
    text = (
        f"{flash_banner}"
        f"{header_title}\n\n"
        f"🏷 <b>شناسه اکانت:</b> <code>{safe_email}</code>\n"
        f"📊 <b>حجم باقیمانده فعلی:</b> <b>{sub.remaining_gb} گیگابایت</b>\n\n"
        f"لطفاً بسته مد نظر خود را جهت {'ارتقا' if is_upgrade else 'تمدید'} انتخاب فرمایید:\n"
        f"{guide_note}"
    )
    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "sub_renew_cancel")
async def handle_sub_renew_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("عملیات تمدید لغو شد.")
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("عملیات تمدید لغو شد.")


@router.callback_query(F.data.startswith("sub_renew_prod:") | F.data.startswith("rnp:"))
async def handle_sub_renew_pick_product(
    callback: CallbackQuery,
    db_user: User,
    db_session: AsyncSession,
):
    """User picks product for renewal; show payment options (Wallet vs Card-to-Card) with compact callbacks."""
    parts = callback.data.split(":")
    if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
        return await callback.answer("اطلاعات تمدید نامعتبر است.", show_alert=True)

    sub, product = await _resolve_sub_and_prod(parts[1], parts[2], db_user, db_session)
    if not sub or sub.user_id != db_user.id:
        return await callback.answer("سرویس یافت نشد یا دسترسی غیرمجاز است.", show_alert=True)
    if not product or not product.is_active:
        return await callback.answer("پلن انتخابی معتبر نیست.", show_alert=True)

    price_toman = product.price_toman
    if db_user.role == UserRole.RESELLER:
        reseller_pct = await ResellerService.get_reseller_discount_percent(db_user.id, db_session)
        discount = (price_toman * reseller_pct) // 100
        price_toman = max(0, price_toman - discount)
    else:
        from app.services.flash_sale_service import FlashSaleService
        price_toman = FlashSaleService.calculate_discounted_price(price_toman)

    wallet_res = await db_session.execute(select(Wallet).where(Wallet.user_id == db_user.id))
    wallet = wallet_res.scalar_one_or_none()
    balance_toman = wallet.balance_toman if wallet else 0

    compact_sub = sub.id.hex[:12]
    compact_prod = product.id.hex[:12]

    payment_buttons = []
    # Option 1: Instant Wallet Renewal if sufficient balance (under 30 bytes)
    if balance_toman >= price_toman:
        payment_buttons.append([
            InlineKeyboardButton(
                text=f"⚡ تمدید آنی با موجودی کیف پول ({price_toman:,} تومان)",
                callback_data=f"rnw:{compact_sub}:{compact_prod}",
            )
        ])
    else:
        payment_buttons.append([
            InlineKeyboardButton(
                text=f"💳 موجودی کیف پول: {balance_toman:,} تومان (ناکافی)",
                callback_data="wallet_topup_start",
            )
        ])

    # Option 2: Card to Card (only if enabled in payment settings)
    if getattr(settings, "CARD_TO_CARD_ENABLED", True):
        payment_buttons.append([
            InlineKeyboardButton(
                text="💳 پرداخت کارت به کارت جهت تمدید",
                callback_data=f"rnc:{compact_sub}:{compact_prod}",
            )
        ])
    payment_buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"sub_renew:{compact_sub}")])

    safe_email = html.escape(sub.client_email or "")
    safe_prod_name = html.escape(product.name or "")
    text = (
        f"🔄 <b>پیش‌فاکتور تمدید سرویس:</b>\n\n"
        f"🏷 <b>سرویس:</b> <code>{safe_email}</code>\n"
        f"📦 <b>بسته انتخابی:</b> <b>{safe_prod_name}</b> ({product.traffic_gb} گیگابایت - {product.duration_days} روز)\n"
        f"💰 <b>مبلغ قابل پرداخت:</b> <b>{price_toman:,} تومان</b>\n"
        f"💳 <b>موجودی کیف پول شما:</b> <b>{balance_toman:,} تومان</b>\n\n"
        f"لطفاً نحوه پرداخت را انتخاب فرمایید:"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=payment_buttons), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("sub_renew_wallet:") | F.data.startswith("rnw:"))
async def handle_sub_renew_wallet(
    callback: CallbackQuery,
    db_user: User,
    db_session: AsyncSession,
):
    """Instant renewal by deducting customer's wallet balance."""
    parts = callback.data.split(":")
    if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
        return await callback.answer("اطلاعات تمدید نامعتبر است.", show_alert=True)

    sub, product = await _resolve_sub_and_prod(parts[1], parts[2], db_user, db_session)
    if not sub or sub.user_id != db_user.id:
        return await callback.answer("سرویس یافت نشد.", show_alert=True)
    if not product or not product.is_active:
        return await callback.answer("پلن نامعتبر است.", show_alert=True)

    price_toman = product.price_toman
    if db_user.role == UserRole.RESELLER:
        reseller_pct = await ResellerService.get_reseller_discount_percent(db_user.id, db_session)
        discount = (price_toman * reseller_pct) // 100
        price_toman = max(0, price_toman - discount)
    else:
        from app.services.flash_sale_service import FlashSaleService
        price_toman = FlashSaleService.calculate_discounted_price(price_toman)

    price_irr = price_toman * 10
    wallet_svc = WalletService(db_session)
    sub_svc = SubscriptionService(db_session)

    try:
        # Deduct wallet balance
        await wallet_svc.deduct(
            user_id=db_user.id,
            amount_irr=price_irr,
            order_id=f"REN-{sub.id.hex[:8]}",
            reason="RENEWAL",
        )

        # Renew subscription on panel and DB (adds traffic to quota)
        renewed_sub = await sub_svc.renew_subscription(
            subscription_id=sub.id,
            product_id=product.id,
        )

        # Create PAID Payment record
        payment = Payment(
            id=uuid.uuid4(),
            order_id=None,
            user_id=db_user.id,
            amount_irr=price_irr,
            amount_toman=price_toman,
            provider=PaymentMethod.WALLET,
            status=PaymentStatus.PAID,
            paid_at=datetime.now(timezone.utc),
            tracking_code=f"WAL-REN-{uuid.uuid4().hex[:6].upper()}",
            admin_notes=f"RENEWAL:{sub.id}:{product.id}",
        )
        db_session.add(payment)
        await db_session.commit()

        # Fetch connection details
        conn = await sub_svc.get_connection_details(renewed_sub.id)
        expiry_str = renewed_sub.expires_at.strftime("%Y-%m-%d %H:%M") if renewed_sub.expires_at else "نامحدود"

        safe_email = html.escape(renewed_sub.client_email or "")
        safe_prod_name = html.escape(product.name or "")
        safe_sub_url = html.escape(conn.get('subscription_url') or "")

        msg = (
            f"🎉 <b>سرویس شما با موفقیت تمدید شد!</b>\n\n"
            f"🏷 <b>شناسه اکانت:</b> <code>{safe_email}</code>\n"
            f"📦 <b>طرح تمدید شده:</b> <b>{safe_prod_name}</b>\n"
            f"📊 <b>حجم کل جدید:</b> <b>{renewed_sub.traffic_limit_gb} گیگابایت</b>\n"
            f"⏳ <b>تاریخ انقضای جدید:</b> <b>{expiry_str}</b>\n"
            f"💰 <b>مبلغ کسر شده از کیف پول:</b> <b>{price_toman:,} تومان</b>\n\n"
            f"💡 <b>توجه مهم:</b> کانفیگ‌ها و بارکد اتصال قبلی شما همچنان فعال و معتبر هستند و نیازی به تغییر یا وارد کردن کانفیگ جدید در نرم‌افزار نیست!\n\n"
            f"🔗 <b>لینک اشتراک هوشمند:</b>\n<code>{safe_sub_url}</code>"
        )
        await callback.message.edit_text(msg, parse_mode="HTML")
        await callback.answer("✅ سرویس با موفقیت تمدید شد!", show_alert=True)
    except Exception as e:
        logger.error(f"Error during wallet renewal for sub {sub.id}: {e}", exc_info=True)
        await callback.answer(f"خطا در تمدید: {e}", show_alert=True)


@router.callback_query(F.data.startswith("sub_renew_c2c:") | F.data.startswith("rnc:"))
async def handle_sub_renew_c2c(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: User,
    db_session: AsyncSession,
):
    """User chooses Card-to-Card payment for renewal; create pending Payment with admin_notes."""
    parts = callback.data.split(":")
    if len(parts) < 3 or not parts[1].strip() or not parts[2].strip():
        return await callback.answer("اطلاعات تمدید نامعتبر است.", show_alert=True)

    sub, product = await _resolve_sub_and_prod(parts[1], parts[2], db_user, db_session)
    if not sub or sub.user_id != db_user.id:
        return await callback.answer("سرویس یافت نشد.", show_alert=True)
    if not product or not product.is_active:
        return await callback.answer("پلن نامعتبر است.", show_alert=True)

    price_toman = product.price_toman
    if db_user.role == UserRole.RESELLER:
        reseller_pct = await ResellerService.get_reseller_discount_percent(db_user.id, db_session)
        discount = (price_toman * reseller_pct) // 100
        price_toman = max(0, price_toman - discount)
    else:
        from app.services.flash_sale_service import FlashSaleService
        price_toman = FlashSaleService.calculate_discounted_price(price_toman)

    price_irr = price_toman * 10
    payment_id = uuid.uuid4()
    tracking_code = f"REN-{secrets.token_hex(4).upper()}"

    payment = Payment(
        id=payment_id,
        order_id=None,
        user_id=db_user.id,
        amount_irr=price_irr,
        provider=PaymentMethod.CARD_TO_CARD,
        status=PaymentStatus.PENDING,
        tracking_code=tracking_code,
        admin_notes=f"RENEWAL:{sub.id}:{product.id}",
    )
    db_session.add(payment)
    await db_session.commit()

    await state.set_state(SubRenewState.awaiting_receipt)
    await state.update_data(payment_id=str(payment.id), sub_id=str(sub.id), product_id=str(product.id))

    card_info = await get_active_bank_card_or_fallback(db_session)
    card_number = card_info["card_number"]
    card_bank_name = card_info["bank_name"]
    card_holder = card_info["card_holder"]

    safe_email = html.escape(sub.client_email or "")
    safe_prod_name = html.escape(product.name or "")
    safe_bank = html.escape(card_bank_name or "")
    safe_holder = html.escape(card_holder or "")

    text = (
        f"💳 <b>اطلاعات پرداخت کارت به کارت جهت تمدید اشتراک:</b>\n\n"
        f"🏷 <b>سرویس:</b> <code>{safe_email}</code>\n"
        f"📦 <b>پلن:</b> <b>{safe_prod_name}</b>\n"
        f"🏦 بانک: <b>{safe_bank}</b>\n"
        f"🔢 شماره کارت (جهت کپی لمس نمایید):\n<code>{card_number}</code>\n"
        f"👤 به نام: <b>{safe_holder}</b>\n"
        f"💰 مبلغ دقیق: <b>{price_toman:,} تومان</b>\n"
        f"🔖 شناسه پیگیری سیستم: <code>{payment.tracking_code}</code>\n\n"
        f"📸 لطفاً پس از واریز، <b>تصویر فیش واریزی</b> را در همین بخش ارسال فرمایید:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 کپی شماره کارت", callback_data=f"copy_card:{card_number}")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data="sub_renew_cancel")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer(f"💳 شماره کارت: {card_number}\nبانک {card_bank_name} ({card_holder})", show_alert=True)


@router.callback_query(F.data.startswith("copy_card:"))
async def handle_services_copy_card(callback: CallbackQuery):
    card = callback.data.split(":", 1)[1]
    await callback.answer(f"📋 شماره کارت:\n{card}\n\nجهت واریز مبلغ در اپلیکیشن بانکی آماده است.", show_alert=True)


@router.message(
    SubRenewState.awaiting_receipt,
    F.photo | F.document | (F.text & ~F.text.startswith("/"))
)
async def handle_sub_renew_receipt_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    db_user: User,
    db_session: AsyncSession,
):
    """Customer uploads receipt for subscription renewal."""
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
                Payment.admin_notes.like("RENEWAL:%"),
            )
            .order_by(Payment.created_at.desc())
        )
        payment = pay_res.scalars().first()

    if not payment:
        await state.clear()
        return await message.answer(
            "⚠️ درخواست تمدید معتبری در انتظار پرداخت یافت نشد. لطفاً از بخش منوی سرویس‌ها مجدداً اقدام فرمایید.",
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
        return await message.answer("⚠️ لطفاً تصویر فیش یا کد رهگیری را ارسال نمایید.")

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

    await message.answer(
        f"✅ <b>رسید پرداخت تمدید با موفقیت ثبت شد.</b>\n\n"
        f"💰 <b>مبلغ فیش:</b> <b>{payment.amount_toman:,} تومان</b>\n"
        f"🔖 <b>شناسه پیگیری:</b> <code>{payment.tracking_code}</code>\n\n"
        f"⏳ فیش برای بررسی و تایید مدیریت ارسال گردید. به محض تایید توسط ادمین، اشتراک شما به صورت خودکار تمدید شده و به شما اطلاع داده خواهد شد.",
        parse_mode="HTML",
    )

    # Broadcast approval card to admins
    u_username = f"@{html.escape(db_user.username)}" if db_user.username else "ندارد"
    u_name = html.escape(db_user.first_name or "کاربر")

    sub_info = ""
    prod_info = ""
    if payment.admin_notes and payment.admin_notes.startswith("RENEWAL:"):
        parts = payment.admin_notes.split(":")
        if len(parts) >= 3:
            try:
                s_obj = await db_session.get(Subscription, uuid.UUID(parts[1]))
                if s_obj:
                    sub_info = f"\n🏷 <b>سرویس:</b> <code>{html.escape(s_obj.client_email or '')}</code>"
                p_obj = await db_session.get(Product, uuid.UUID(parts[2]))
                if p_obj:
                    prod_info = f"\n📦 <b>بسته تمدید:</b> {html.escape(p_obj.name or '')}"
            except Exception:
                pass

    admin_caption = (
        f"🔔 <b>درخواست تمدید سرویس جدید (کارت به کارت)</b>\n\n"
        f"👤 <b>کاربر:</b> {u_name} ({u_username})\n"
        f"🆔 <b>شناسه کاربر:</b> <code>{db_user.telegram_id}</code>{sub_info}{prod_info}\n"
        f"💰 <b>مبلغ تمدید:</b> <b>{payment.amount_toman:,} تومان</b>\n"
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
    logger.info(f"Broadcasted renewal receipt {payment.tracking_code} to all admins.")


@router.callback_query(F.data.startswith("sub_sync:"))
async def handle_sub_sync(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)
    try:
        sub_id = uuid.UUID(parts[1].strip())
    except ValueError:
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    sub = await db_session.get(Subscription, sub_id)
    if not sub or sub.user_id != db_user.id:
        return await callback.answer("سرویس یافت نشد یا دسترسی غیرمجاز است.", show_alert=True)

    from app.database.models.panel import Panel
    from app.integrations.xui.client import ThreeXUIClient
    from app.integrations.xui.adapter import ThreeXUIPanelAdapter

    panel = await db_session.get(Panel, sub.panel_id)
    if not panel:
        return await callback.answer("سرور در دسترس نیست.", show_alert=True)

    client = ThreeXUIClient(
        base_url=panel.base_url,
        username=panel.username,
        password=panel.password,
        api_token=panel.api_token,
        login_secret=panel.login_secret,
        timeout=10.0,
    )
    adapter = ThreeXUIPanelAdapter(client)
    try:
        stats = await adapter.get_client_traffic(sub.client_email)
        if stats:
            sub.traffic_used_bytes = stats.up + stats.down
            await db_session.commit()
            await callback.answer(
                f"📊 مصرف ترافیک به‌روز شد:\nمصرف: {sub.traffic_used_gb} GB\nکل: {sub.traffic_limit_gb} GB\nباقیمانده: {sub.remaining_gb} GB",
                show_alert=True,
            )
        else:
            await callback.answer("اطلاعات مصرف در پنل یافت نشد.", show_alert=True)
    except Exception as e:
        await callback.answer(f"خطا در استعلام از پنل: {e}", show_alert=True)
    finally:
        await client.close()


@router.callback_query(F.data.startswith("sub_delete_ask:"))
async def handle_sub_delete_ask(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Show prominent confirmation warning before deleting a subscription."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)
    try:
        sub_id = uuid.UUID(parts[1].strip())
    except ValueError:
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    sub = await db_session.get(Subscription, sub_id)
    if not sub or sub.user_id != db_user.id:
        return await callback.answer("سرویس یافت نشد یا دسترسی غیرمجاز است.", show_alert=True)

    server = await db_session.get(Server, sub.server_id) if sub.server_id else None
    loc_name = html.escape(server.location_name if server else "نامشخص")
    safe_email = html.escape(sub.client_email or "")

    text = (
        f"⚠️ <b>هشدار حذف دائم سرویس!</b>\n\n"
        f"آیا واقعاً از حذف سرویس زیر اطمینان دارید؟\n"
        f"🏷 <b>شناسه اکانت:</b> <code>{safe_email}</code>\n"
        f"🌍 <b>لوکیشن:</b> <b>{loc_name}</b>\n"
        f"📊 <b>حجم باقیمانده:</b> <b>{sub.remaining_gb} گیگابایت</b>\n\n"
        f"❌ <b>توجه مهم:</b> با تایید حذف، این اکانت از پنل سرور و ربات به صورت کامل حذف شده و اتصال شما قطع خواهد شد. <b>این عملیات غیرقابل بازگشت است!</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 بله، مطمئنم! این سرویس را حذف کن", callback_data=f"sub_delete_confirm:{sub.id}"),
        ],
        [
            InlineKeyboardButton(text="🔙 انصراف و بازگشت", callback_data="sub_delete_cancel"),
        ],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "sub_delete_cancel")
async def handle_sub_delete_cancel(callback: CallbackQuery):
    """Cancel subscription deletion and delete prompt."""
    await callback.answer("عملیات حذف لغو شد.")
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("عملیات حذف لغو شد.")


@router.callback_query(F.data.startswith("sub_delete_confirm:"))
async def handle_sub_delete_confirm(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    """Execute subscription deletion from 3X-UI panel and mark cancelled in DB."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)
    try:
        sub_id = uuid.UUID(parts[1].strip())
    except ValueError:
        return await callback.answer("شناسه اشتراک نامعتبر است.", show_alert=True)

    sub = await db_session.get(Subscription, sub_id)
    if not sub or sub.user_id != db_user.id:
        return await callback.answer("سرویس یافت نشد یا دسترسی غیرمجاز است.", show_alert=True)

    from app.database.models.panel import Panel, Inbound
    from app.integrations.xui.client import ThreeXUIClient
    from app.integrations.xui.adapter import ThreeXUIPanelAdapter

    # Attempt to delete client from panel across all inbounds
    panel = await db_session.get(Panel, sub.panel_id) if sub.panel_id else None
    if panel:
        panel_inbs = (await db_session.execute(select(Inbound).where(Inbound.panel_id == panel.id))).scalars().all()
        all_remote_ids = [ib.remote_inbound_id for ib in panel_inbs if ib.remote_inbound_id]
        client = ThreeXUIClient(
            base_url=panel.base_url,
            username=panel.username,
            password=panel.password,
            api_token=panel.api_token,
            login_secret=panel.login_secret,
            timeout=10.0,
        )
        adapter = ThreeXUIPanelAdapter(client)
        try:
            inbound = await db_session.get(Inbound, sub.inbound_id) if sub.inbound_id else None
            remote_inbound_id = inbound.remote_inbound_id if inbound else 1
            await adapter.delete_client(
                remote_inbound_id,
                sub.client_uuid,
                client_email=sub.client_email,
                all_inbound_ids=all_remote_ids,
            )
        except Exception as exc:
            logger.warning(f"Failed to delete client {sub.client_email} on panel: {exc}")
        finally:
            await client.close()

    safe_del_email = html.escape(sub.client_email or "")
    await db_session.delete(sub)
    await db_session.commit()

    await callback.answer("✅ سرویس با موفقیت حذف گردید.", show_alert=True)
    await callback.message.edit_text(
        f"🗑 <b>سرویس با موفقیت حذف شد:</b>\n<code>{safe_del_email}</code>\n\nاین اکانت از پنل و ربات خارج گردید.",
        parse_mode="HTML",
    )


@router.message(F.text == MAIN_MENU_BTNS.get("reseller", "💼 پنل نمایندگی"))
async def open_reseller_panel(message: Message, db_user: User, db_session: AsyncSession):
    if db_user.role != UserRole.RESELLER:
        return await message.answer("⚠️ این بخش مخصوص همکاران و نمایندگان فروش است.")

    from app.modules.wallets.service import WalletService
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    wallet_svc = WalletService(db_session)
    wallet = await wallet_svc.get_or_create_wallet(db_user.id)

    subs_cnt = (await db_session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.user_id == db_user.id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
    )).scalar_one()

    safe_first_name = html.escape(db_user.first_name or "همکار")
    reseller_pct = await ResellerService.get_reseller_discount_percent(db_user.id, db_session)
    text = (
        f"💼 <b>پنل همکار و نماینده فروش VIP:</b>\n\n"
        f"👤 نماینده گرامی: <b>{safe_first_name}</b>\n"
        f"🆔 شناسه کاربری: <code>{db_user.telegram_id}</code>\n"
        f"💳 موجودی کیف پول شما: <b>{wallet.balance_toman:,} تومان</b>\n"
        f"💎 درصد تخفیف همکاری: <b>{reseller_pct}% روی تمامی پلن‌ها</b>\n"
        f"📦 اشتراک‌های فعال مشتریان شما: <b>{subs_cnt} عدد</b>\n\n"
        f"<i>کلیه خریدهای شما از طریق کیف پول به صورت خودکار با {reseller_pct}٪ تخفیف محاسبه و تحویل داده می‌شود.</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛒 خرید سرویس برای مشتری با تخفیف", callback_data="reseller_start_buy"),
        ],
        [
            InlineKeyboardButton(text="💳 شارژ کیف پول نماینده", callback_data="wallet_topup"),
        ],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "reseller_start_buy")
async def reseller_start_buy(callback: CallbackQuery, db_user: User, db_session: AsyncSession):
    from app.database.models.product import Product
    from app.bot.keyboards.inline import get_products_keyboard
    products_res = await db_session.execute(
        select(Product).where(Product.is_active.is_(True)).order_by(Product.sort_order.asc())
    )
    products = list(products_res.scalars().all())
    await callback.message.answer(
        "🛒 <b>لطفاً پلن مورد نظر برای مشتری خود را انتخاب فرمایید (تخفیف نمایندگی در فاکتور اعمال خواهد شد):</b>",
        reply_markup=get_products_keyboard(products),
        parse_mode="HTML"
    )
    await callback.answer()

