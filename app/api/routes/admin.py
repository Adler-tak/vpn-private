"""Admin API Routes for standalone Web Admin Panel."""
import asyncio
import json
import os
import shutil
import uuid
import time
import secrets
import logging
from pathlib import Path
import bcrypt
import ipaddress
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status, BackgroundTasks, UploadFile, File, Form
from app.modules.vouchers.excel import (
    generate_voucher_template_excel,
    parse_vouchers_from_bytes,
    export_unused_vouchers_excel,
    calculate_duration_days,
)
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, desc
from sqlalchemy.orm import selectinload
from aiogram import Bot

try:
    import psutil
except ImportError:
    psutil = None

from app.core.config import settings, get_live_env, update_env_variable
from app.core.exceptions import VPNPrivateError, InsufficientFundsError
from app.core.constants import UserRole, PaymentStatus, OrderStatus, SubscriptionStatus, TicketStatus, PaymentMethod
from app.database.session import get_db
from app.database.models.user import User, Wallet
from app.database.models.order import Order
from app.database.models.payment import Payment, PaymentReceipt, BankCard, PaymentGateway
from app.database.models.subscription import Subscription
from app.database.models.panel import Panel, Server, Inbound
from app.database.models.product import Product
from app.database.models.support import SupportTicket, SupportMessage
from app.database.models.accounting import AccountingEntry
from app.database.models.audit import AuditLog
from app.database.models.reseller import Reseller, Coupon, Referral, LuckyWheelSpin
from app.services.reseller_service import ResellerService
from app.services.lucky_wheel_service import LuckyWheelService
from app.services.settings_service import SettingsService
from app.database.models.voucher import Voucher
from app.database.models.bot import TelegramBot
import httpx
from app.core.totp import (
    generate_totp_secret,
    get_totp_code,
    verify_totp,
    generate_otpauth_uri,
    generate_qr_code_base64,
)

from app.integrations.factory import get_panel_adapter, get_panel_client
from app.integrations.xui.client import ThreeXUIClient
from app.integrations.xui.adapter import ThreeXUIPanelAdapter
from app.integrations.xui.schemas import ClientSpec

from app.modules.orders.service import OrderService
from app.modules.subscriptions.service import SubscriptionService
from app.modules.wallets.service import WalletService
from app.modules.support.service import SupportService
from app.modules.vouchers.service import VoucherService
from app.modules.vouchers.printer import render_printable_vouchers_html
from app.core.exceptions import VPNPrivateError
from app.core.cache import security_store
from app.api.auth import create_admin_token, get_current_admin, revoke_token, revoke_all_tokens, bearer_scheme
from app.bot.handlers.admin import generate_qr_buffer
from app.bot.notifications import notify_all_admins
from aiogram.types import BufferedInputFile

logger = logging.getLogger(__name__)

router = APIRouter()


def get_telegram_bot() -> Bot:
    return Bot(token=settings.BOT_TOKEN)


async def get_optional_admin(
    request: Request,
    auth_header: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    token = None
    if auth_header and auth_header.credentials:
        token = auth_header.credentials
    elif "admin_token" in request.cookies:
        token = request.cookies.get("admin_token")
    elif "token" in request.query_params:
        token = request.query_params.get("token")
    if not token:
        return None
    try:
        return await get_current_admin(request, auth_header, db)
    except Exception:
        return None


# -------------------------------------------------------------
# Pydantic Schemas
# -------------------------------------------------------------
class AdminLoginRequest(BaseModel):
    username: str
    password: str


class TelegramBotCreateRequest(BaseModel):
    token: str
    name: Optional[str] = None


class VoucherGenerateRequest(BaseModel):
    count: int = Field(..., ge=1, le=500, description="تعداد ووچرهای تولیدی (بین ۱ تا ۵۰۰)")
    voucher_type: str = Field("plan", description="نوع ووچر: plan یا wallet_credit")
    amount_toman: Optional[int] = Field(0, description="مبلغ شارژ به تومان در صورت انتخاب نوع wallet_credit")
    product_id: Optional[Any] = Field(None, description="شناسه محصول در صورت انتخاب نوع plan")
    duration_days: Optional[int] = Field(None, ge=0, description="مدت اعتبار ووچر به روز (۰ = نامحدود)")
    expires_in_days: Optional[int] = Field(None, ge=0, description="نام مستعار برای duration_days")


class VoucherImportRequest(BaseModel):
    codes: List[str] = Field(..., description="فهرست کدهای ووچر برای ایمپورت")
    voucher_type: str = Field("wallet_credit", description="نوع: wallet_credit یا plan")
    amount_toman: Optional[int] = Field(0, description="مبلغ شارژ به تومان")
    product_id: Optional[Any] = Field(None, description="شناسه محصول در صورت انتخاب plan")
    duration_days: Optional[int] = Field(180, description="مدت اعتبار به روز")


class VoucherManualAddRequest(BaseModel):
    mode: str = Field("single", description="single or bulk")
    duration_months: Optional[int] = Field(1, description="مدت زمان به ماه")
    username: Optional[str] = Field(None, description="نام کاربری")
    password: Optional[str] = Field(None, description="رمز عبور")
    text_data: Optional[str] = Field(None, description="متن چند خطی برای افزودن گروهی")
    product_id: Optional[Any] = Field(None, description="شناسه محصول یا auto")


class VoucherUpdateRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    duration_months: Optional[int] = None
    product_id: Optional[Any] = None
    is_used: Optional[bool] = None


class TelegramBotProxyUpdateRequest(BaseModel):
    proxy_url: Optional[str] = None
    proxy_enabled: bool = False


class TelegramBotProxyTestRequest(BaseModel):
    proxy_url: str


class AdjustBalanceRequest(BaseModel):
    amount_toman: int = Field(..., description="Amount in Toman (positive to credit, negative to debit)")
    reason: Optional[str] = "تغییر موجودی از طریق پنل وب"


class UpdateUserRoleRequest(BaseModel):
    role: str


class TicketReplyRequest(BaseModel):
    message: str
    close_after_reply: bool = False


class ProductUpdateRequest(BaseModel):
    name: Optional[str] = None
    price_toman: Optional[int] = Field(None, ge=0, description="Price in Toman (non-negative)")
    traffic_gb: Optional[float] = Field(None, ge=0, description="Volume in GB, 0 = unlimited")
    duration_days: Optional[int] = Field(None, ge=1, description="Duration in days (at least 1)")
    protocol: Optional[str] = None
    allowed_locations: Optional[str] = None
    location_mode: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class ProductCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="Product name")
    traffic_gb: float = Field(..., ge=0, description="Volume in GB, 0 = unlimited")
    duration_days: int = Field(..., ge=1, description="Duration in days (at least 1)")
    price_toman: int = Field(..., ge=0, description="Price in Toman (non-negative)")
    protocol: str = "vless"
    allowed_locations: str = "ALL"
    location_mode: str = "all"
    is_active: bool = True
    sort_order: int = 0


class QuickCreateSubscriptionRequest(BaseModel):
    remark: Optional[str] = None
    traffic_gb: float = Field(0.0, ge=0, description="حجم ترافیک به گیگابایت (بزرگتر یا مساوی ۰)")
    duration_days: int = Field(30, ge=0, description="مدت زمان به روز (بزرگتر یا مساوی ۰)")
    server_id: Optional[str] = None
    server_ids: Optional[List[str]] = None
    protocol: str = Field("vless", description="پروتکل اتصال")
    user_id: Optional[str] = None


class ExtendSubscriptionRequest(BaseModel):
    add_traffic_gb: float = Field(0, ge=0, description="Additional traffic in GB")
    add_days: int = Field(0, ge=0, description="Additional days")


class ChangeLocationRequest(BaseModel):
    server_id: str


class AssignSubscriptionUserRequest(BaseModel):
    user_identifier: str = Field("", description="Telegram numeric ID, @username, or empty to unassign back to manual web-panel")
    customer_name: Optional[str] = Field(None, description="Optional customer display name if creating new user")


class CouponCreateRequest(BaseModel):
    code: str = Field(..., min_length=1, description="Coupon code")
    discount_percent: int = Field(0, ge=0, le=100, description="Discount percent (0-100)")
    discount_amount_toman: int = Field(0, ge=0, description="Discount amount in Toman")
    max_uses: int = Field(100, ge=1, description="Max uses (at least 1)")
    valid_days: int = Field(30, ge=1, description="Valid days (at least 1)")


class PaymentDetailsUpdateRequest(BaseModel):
    card_number: Optional[str] = None
    card_holder: Optional[str] = None
    card_bank_name: Optional[str] = None
    card_iban: Optional[str] = None
    crypto_wallet_usdt: Optional[str] = None
    crypto_wallet_trc20: Optional[str] = None
    crypto_wallet_bep20: Optional[str] = None
    card_to_card_enabled: Optional[bool] = None
    crypto_enabled: Optional[bool] = None
    crypto_trc20_enabled: Optional[bool] = None
    crypto_bep20_enabled: Optional[bool] = None
    usdt_toman_rate: Optional[int] = None
    zarinpal_enabled: Optional[bool] = None
    zarinpal_merchant_id: Optional[str] = None
    stripe_enabled: Optional[bool] = None
    stripe_publishable_key: Optional[str] = None
    stripe_secret_key: Optional[str] = None
    stripe_currency: Optional[str] = None
    stripe_exchange_rate: Optional[int] = None
    min_wallet_topup_toman: Optional[int] = None


class BankCardCreateRequest(BaseModel):
    card_number: str
    card_holder: str
    bank_name: str
    iban: Optional[str] = None


class PaymentGatewayCreateRequest(BaseModel):
    provider: str
    title: str
    config: Dict[str, Any] = {}
    is_active: bool = True
    sort_order: int = 0


class PaymentGatewayUpdateRequest(BaseModel):
    title: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class BroadcastRequest(BaseModel):
    message: str
    target_audience: str = "all"  # "all" | "active_subs" | "expired_subs"


class DirectMessageRequest(BaseModel):
    message: str



class RestoreBackupRequest(BaseModel):
    filename: str


class SystemSettingsUpdateRequest(BaseModel):
    trial_enabled: Optional[bool] = None
    trial_traffic_mb: Optional[int] = None
    trial_duration_days: Optional[int] = None
    trial_max_count: Optional[int] = None
    trial_inbound_id: Optional[str] = None
    card_number: Optional[str] = None
    card_holder: Optional[str] = None
    card_bank_name: Optional[str] = None
    card_to_card_enabled: Optional[bool] = None
    referral_commission_percent: Optional[int] = None
    health_check_interval: Optional[int] = None
    health_retry_count: Optional[int] = None
    health_retry_interval: Optional[int] = None
    health_alert_cooldown: Optional[int] = None


class HealthMonitorSettingsRequest(BaseModel):
    check_interval_seconds: Optional[int] = Field(None, ge=10, le=3600, description="بازه بررسی دوره‌ای سلامت پنل‌ها به ثانیه")
    retry_count: Optional[int] = Field(None, ge=1, le=20, description="تعداد دفعات تست مجدد متوالی قبل از ارسال هشدار")
    retry_interval_seconds: Optional[int] = Field(None, ge=1, le=120, description="فاصله زمانی بین هر تلاش مجدد به ثانیه")
    alert_cooldown_minutes: Optional[int] = Field(None, ge=1, le=1440, description="حداقل فاصله زمانی بین ارسال هشدارهای تکراری به دقیقه")


class BotTextsUpdateRequest(BaseModel):
    texts: Optional[Dict[str, str]] = None


class ReferralSettingsUpdateRequest(BaseModel):
    commission_percent: int = Field(..., ge=0, le=100, description="درصد پورسانت معرف (۰ تا ۱۰۰)")


class AddBotAdminRequest(BaseModel):
    identifier: str


class AdminSecurityUpdateRequest(BaseModel):
    current_password: str = Field(..., description="رمز عبور فعلی مدیریت جهت اعتبارسنجی تغییرات")
    admin_web_username: Optional[str] = None
    admin_web_password: Optional[str] = None
    admin_2fa_enabled: Optional[bool] = None
    admin_2fa_telegram_id: Optional[int] = None


class ChangeUsernameRequest(BaseModel):
    new_username: str = Field(..., min_length=3, max_length=64, description="نام کاربری جدید مدیریت")
    current_password: Optional[str] = Field(None, description="رمز عبور فعلی جهت اعتبارسنجی (اختیاری)")


class PanelCreateRequest(BaseModel):
    name: str = Field(..., description="Display name for panel / server")
    base_url: str = Field(..., description="Full URL with port, e.g. http://185.x.x.x:2053")
    panel_type: Optional[str] = Field("3x-ui", description="Panel type: 3x-ui or vpn-ui")
    username: str = Field(..., description="3X-UI admin username")
    password: str = Field(..., description="3X-UI admin password")
    api_token: Optional[str] = Field(None, description="Optional 3X-UI Bearer API token")
    login_secret: Optional[str] = Field(None, description="Optional 2FA LoginSecret / TOTP Base32 Key")
    otp_secret: Optional[str] = Field(None, description="Alias for login_secret")
    version: Optional[str] = "v3.11"


class PanelUpdateRequest(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    panel_type: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    api_token: Optional[str] = None
    login_secret: Optional[str] = None
    otp_secret: Optional[str] = None
    version: Optional[str] = None


# Backward-compatible references for unit tests and internal state
_failed_login_attempts = security_store._failed_attempts
_blocked_ips = security_store._blocked_ips
_pending_otp_sessions = security_store._otp_sessions


class Verify2FARequest(BaseModel):
    session_token: str
    otp_code: str


def _is_trusted_proxy_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def _get_client_ip(request: Request) -> str:
    direct_peer = request.client.host if request.client else "127.0.0.1"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and _is_trusted_proxy_ip(direct_peer):
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    return direct_peer


async def record_audit(
    db: AsyncSession,
    action: str,
    details: str = "",
    actor_id: Optional[Any] = None,
    actor_role: str = "ADMIN",
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> AuditLog:
    """Helper to record audit trail entries across admin operations."""
    int_actor_id = None
    if isinstance(actor_id, int):
        int_actor_id = actor_id
    elif isinstance(actor_id, str) and actor_id.isdigit():
        int_actor_id = int(actor_id)
    elif hasattr(actor_id, "telegram_id"):
        int_actor_id = actor_id.telegram_id

    entry = AuditLog(
        id=uuid.uuid4(),
        actor_id=int_actor_id,
        actor_role=actor_role,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
        details=details,
        ip_address=ip_address,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    return entry



# -------------------------------------------------------------
# 1. Authentication (Hashed Bcrypt + Rate Limiting + Telegram 2FA)
# -------------------------------------------------------------
@router.post("/login")
async def admin_login(
    req: AdminLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Admin Login with username/password, Bcrypt verification, Rate Limiting & 2FA."""
    now = time.time()
    client_ip = _get_client_ip(request)

    # 1. Check if IP is currently blocked
    is_blocked, remaining = await security_store.check_ip_blocked(client_ip)
    if is_blocked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"دسترسی شما به دلیل تلاش‌های ناموفق مکرر تا {remaining} ثانیه دیگر مسدود است.",
        )

    u = req.username.strip()
    p = req.password.strip()

    valid = False
    admin_tg_id = settings.ADMIN_2FA_TELEGRAM_ID or (settings.ADMIN_TELEGRAM_IDS[0] if settings.ADMIN_TELEGRAM_IDS else 0)
    username_found = settings.ADMIN_WEB_USERNAME

    # Match primary admin against DB, dynamic settings, json store, or .env
    from app.core.config import _load_system_settings_json
    import os
    json_settings = _load_system_settings_json()

    valid_usernames = {
        settings.ADMIN_WEB_USERNAME.lower(),
        str(json_settings.get("ADMIN_WEB_USERNAME", "")).lower(),
        str(os.environ.get("ADMIN_WEB_USERNAME", "")).lower(),
    }
    from app.services.settings_service import SettingsService
    db_user = await SettingsService.get_value("ADMIN_WEB_USERNAME")
    db_pass = await SettingsService.get_value("ADMIN_WEB_PASSWORD")
    db_hash = await SettingsService.get_value("ADMIN_WEB_PASSWORD_HASH")
    if db_user:
        valid_usernames.add(str(db_user).lower().strip())
    valid_usernames.discard("")

    is_primary_username = (
        u.lower() in valid_usernames
        or (u.isdigit() and int(u) in settings.ADMIN_TELEGRAM_IDS)
    )

    if is_primary_username:
        expected_passwords = {
            settings.ADMIN_WEB_PASSWORD,
            str(json_settings.get("ADMIN_WEB_PASSWORD", "")),
            str(os.environ.get("ADMIN_WEB_PASSWORD", "")),
        }
        if db_pass:
            expected_passwords.add(str(db_pass).strip())
        expected_passwords.discard("")

        expected_hashes = {
            settings.ADMIN_WEB_PASSWORD_HASH,
            str(json_settings.get("ADMIN_WEB_PASSWORD_HASH", "")),
            str(os.environ.get("ADMIN_WEB_PASSWORD_HASH", "")),
        }
        if db_hash:
            expected_hashes.add(str(db_hash).strip())
        expected_hashes.discard("")

        if p in expected_passwords:
            valid = True
            username_found = u
        else:
            for h in expected_hashes:
                if not h:
                    continue
                try:
                    pw_bytes = p.encode("utf-8")
                    hash_bytes = h.replace("$$", "$").encode("utf-8")
                    if bcrypt.checkpw(pw_bytes, hash_bytes):
                        valid = True
                        username_found = u
                        break
                except Exception as e:
                    logger.error(f"Error checking bcrypt hash: {e}")

        # If logged in via installer/json credentials, ensure DB is kept in sync
        if valid:
            try:
                from app.services.settings_service import SettingsService
                if SettingsService.get_value("ADMIN_WEB_USERNAME") != username_found:
                    await SettingsService.set_value("ADMIN_WEB_USERNAME", username_found)
                if p in expected_passwords and SettingsService.get_value("ADMIN_WEB_PASSWORD") != p:
                    await SettingsService.set_value("ADMIN_WEB_PASSWORD", p)
            except Exception as e:
                logger.debug(f"Could not sync admin credentials to DB: {e}")

    # Fallback to DB admin users
    if not valid:
        conditions = [User.username == u.lstrip("@")]
        if u.isdigit():
            conditions.append(User.telegram_id == int(u))
        q = select(User).where(or_(*conditions)).order_by(User.id.desc())
        res = await db.execute(q)
        user_match = res.scalars().first()
        if user_match and user_match.role in [UserRole.ADMIN, UserRole.SUPER_ADMIN, "super_admin", "admin"]:
            if user_match.telegram_id in settings.ADMIN_TELEGRAM_IDS:
                expected_pw = settings.ADMIN_WEB_PASSWORD
                if expected_pw and p == expected_pw:
                    valid = True
                    admin_tg_id = user_match.telegram_id
                    username_found = user_match.username or settings.ADMIN_WEB_USERNAME
                else:
                    try:
                        pw_bytes = p.encode("utf-8")
                        hash_bytes = settings.ADMIN_WEB_PASSWORD_HASH.encode("utf-8")
                        if bcrypt.checkpw(pw_bytes, hash_bytes):
                            valid = True
                            admin_tg_id = user_match.telegram_id
                            username_found = user_match.username or settings.ADMIN_WEB_USERNAME
                    except Exception:
                        pass

    # Handle invalid login credentials
    if not valid:
        fail_count, was_blocked = await security_store.record_login_failure(client_ip)
        if was_blocked:
            try:
                bot = get_telegram_bot()
                primary_admin = settings.ADMIN_TELEGRAM_IDS[0] if settings.ADMIN_TELEGRAM_IDS else None
                if primary_admin:
                    await bot.send_message(
                        chat_id=primary_admin,
                        text=f"🚨 <b>هشدار امنیتی</b>\n\nآی‌پی <code>{client_ip}</code> پس از ۵ بار تلاش ناموفق مسدود شد.",
                        parse_mode="HTML",
                    )
            except Exception:
                pass
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="به دلیل ۵ تلاش ناموفق، آی‌پی شما به مدت ۱۵ دقیقه مسدود شد.",
            )
        remaining_tries = max(0, 5 - fail_count)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"نام کاربری یا رمز عبور اشتباه است. (فرصت‌های باقیمانده: {remaining_tries})",
        )

    # Success! Clear failed attempts
    await security_store.reset_login_failures(client_ip)

    # Two-Factor Authentication via Telegram
    if settings.ADMIN_2FA_ENABLED:
        otp_code = f"{secrets.randbelow(900000) + 100000}"
        session_token = secrets.token_urlsafe(32)
        await security_store.set_otp_session(
            session_token,
            {
                "code": otp_code,
                "attempts": 0,
                "admin_tg_id": admin_tg_id,
                "username": username_found,
                "client_ip": client_ip,
            },
            ttl_seconds=120,
        )

        try:
            bot = get_telegram_bot()
            now_tehran = datetime.now(timezone(timedelta(hours=3, minutes=30))).strftime("%H:%M:%S")
            await bot.send_message(
                chat_id=admin_tg_id,
                text=(
                    "🔐 <b>درخواست ورود به پنل مدیریت وب</b>\n\n"
                    "🔢 <b>کد تأیید ورود شما:</b>\n"
                    f"👉 <code>{otp_code}</code> 👈\n\n"
                    "⏳ <b>اعتبار کد:</b> ۲ دقیقه\n"
                    f"🌐 <b>آی‌پی درخواست:</b> <code>{client_ip}</code>\n"
                    f"🕒 <b>زمان:</b> {now_tehran}\n\n"
                    "⚠️ <i>اگر این درخواست توسط شما انجام نشده است، سریعاً رمز را بررسی نمایید.</i>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send 2FA OTP to Telegram: {e}")
            if not getattr(settings, "ADMIN_TOTP_SECRET", ""):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"خطا در ارسال کد تأیید تلگرام: {str(e)}",
                )

        return {
            "success": True,
            "require_2fa": True,
            "session_token": session_token,
            "message": "کد تأیید ۶ رقمی به تلگرام شما ارسال شد." if not getattr(settings, "ADMIN_TOTP_SECRET", "") else "کد تأیید به تلگرام ارسال شد یا از کد ۶ رقمی Google Authenticator استفاده نمایید.",
        }

    # Direct JWT issue if 2FA disabled
    token = create_admin_token(
        telegram_id=admin_tg_id,
        username=username_found,
        role=UserRole.SUPER_ADMIN,
    )
    is_secure_cookie = (getattr(settings, "ENVIRONMENT", "").lower() == "production") and not getattr(settings, "DEBUG", False)
    response.set_cookie(
        key="admin_token",
        value=token,
        max_age=86400 * 7,
        httponly=True,
        secure=is_secure_cookie,
        samesite="lax",
    )
    return {
        "success": True,
        "require_2fa": False,
        "token": token,
        "user": {
            "telegram_id": admin_tg_id,
            "username": username_found,
            "role": "super_admin",
        },
    }


@router.post("/verify-2fa")
async def verify_2fa(
    req: Verify2FARequest,
    request: Request,
    response: Response,
):
    """Verify 2FA Telegram OTP and issue JWT Admin token."""
    now = time.time()
    client_ip = _get_client_ip(request)
    session = await security_store.get_otp_session(req.session_token)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="نشست ورود منقضی شده یا نامعتبر است. لطفاً مجدداً وارد شوید.",
        )

    code_input = req.otp_code.strip()
    is_valid_otp = (code_input == session.get("code", "").strip())

    # Accept either Telegram OTP code OR valid Google Authenticator TOTP code
    if not is_valid_otp:
        totp_secret = getattr(settings, "ADMIN_TOTP_SECRET", "")
        if totp_secret and verify_totp(totp_secret, code_input):
            is_valid_otp = True

    if not is_valid_otp:
        session["attempts"] = session.get("attempts", 0) + 1
        rem = 3 - session["attempts"]
        if rem <= 0:
            await security_store.delete_otp_session(req.session_token)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="کد تأیید ۳ بار اشتباه وارد شد. نشست باطل گردید.",
            )
        await security_store.set_otp_session(req.session_token, session, ttl_seconds=max(10, int(session.get("expires_at", now + 120) - now)))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"کد تأیید اشتباه است. ({rem} فرصت باقیمانده)",
        )

    # Verification successful!
    admin_tg_id = session["admin_tg_id"]
    username_found = session["username"]
    await security_store.delete_otp_session(req.session_token)

    token = create_admin_token(
        telegram_id=admin_tg_id,
        username=username_found,
        role=UserRole.SUPER_ADMIN,
    )

    is_secure_cookie = (getattr(settings, "ENVIRONMENT", "").lower() == "production") and not getattr(settings, "DEBUG", False)
    response.set_cookie(
        key="admin_token",
        value=token,
        max_age=86400 * 7,
        httponly=True,
        secure=is_secure_cookie,
        samesite="lax",
    )

    # Send success alert to Telegram
    try:
        bot = get_telegram_bot()
        now_tehran = datetime.now(timezone(timedelta(hours=3, minutes=30))).strftime("%H:%M:%S")
        await bot.send_message(
            chat_id=admin_tg_id,
            text=(
                "✅ <b>ورود موفقیت‌آمیز به پنل مدیریت وب</b>\n\n"
                f"👤 نام کاربری: <b>{username_found}</b>\n"
                f"🌐 آی‌پی: <code>{client_ip}</code>\n"
                f"🕒 زمان: {now_tehran}"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    return {
        "success": True,
        "token": token,
        "user": {
            "telegram_id": admin_tg_id,
            "username": username_found,
            "role": "super_admin",
        },
    }


# -------------------------------------------------------------
# Google Authenticator 2FA (RFC 6238 TOTP) & Bot Management
# -------------------------------------------------------------
@router.get("/2fa/setup")
@router.get("/admin/2fa/setup")
async def setup_admin_2fa(
    regenerate: bool = Query(True, description="Whether to generate a fresh secret"),
    admin: Optional[User] = Depends(get_optional_admin),
    db: Optional[AsyncSession] = Depends(get_db),
):
    """Generates a base32 secret and QR code for Google Authenticator 2FA."""
    # Security: If 2FA is active and already configured, require admin authentication
    if settings.ADMIN_2FA_ENABLED and settings.ADMIN_TOTP_SECRET and not admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="احراز هویت دوعاملی فعال است. لطفاً ابتدا وارد حساب مدیریت شوید.",
        )

    if not regenerate and settings.ADMIN_TOTP_SECRET:
        secret = settings.ADMIN_TOTP_SECRET
    else:
        secret = generate_totp_secret()
        settings.ADMIN_TOTP_SECRET = secret
        update_env_variable("ADMIN_TOTP_SECRET", secret)

    otpauth_url = generate_otpauth_uri(
        secret=secret,
        account_name=settings.ADMIN_WEB_USERNAME,
        issuer_name="VPN Private",
    )
    raw_b64 = generate_qr_code_base64(otpauth_url)
    qr_data_uri = f"data:image/png;base64,{raw_b64}" if not raw_b64.startswith("data:") else raw_b64

    if admin and db:
        await record_audit(
            db,
            action="ADMIN_2FA_SETUP",
            details="Generated Google Authenticator 2FA TOTP secret and QR code.",
            actor_id=admin.telegram_id,
        )
        await db.commit()

    return {
        "secret": secret,
        "qr_code_base64": qr_data_uri,
        "qr_base64": qr_data_uri,
        "qr_data_uri": qr_data_uri,
        "otpauth_url": otpauth_url,
    }


@router.get("/telegram-bots")
@router.get("/admin/telegram-bots")
async def list_telegram_bots(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all registered Telegram bots with their status."""
    bots_res = await db.execute(select(TelegramBot).order_by(TelegramBot.created_at.desc()))
    bots = bots_res.scalars().all()
    items = [
        {
            "id": str(b.id),
            "name": b.name,
            "token": b.token,
            "username": b.username,
            "is_active": b.is_active,
            "proxy_url": b.proxy_url,
            "proxy_enabled": bool(b.proxy_enabled),
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in bots
    ]
    from app.services.settings_service import get_effective_bot_username
    eff_bot = await get_effective_bot_username(db)
    return {
        "success": True,
        "bots": items,
        "items": items,
        "total": len(items),
        "active_bot_username": eff_bot,
        "default_bot_username": "VPNPrivateSell_Bot",
    }


@router.post("/telegram-bots")
@router.post("/admin/telegram-bots")
async def add_telegram_bot(
    req: TelegramBotCreateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add new bot token. Query https://api.telegram.org/bot{token}/getMe via httpx to validate and auto-extract bot username and name."""
    raw_token = req.token.strip()
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="توکن ربات تلگرام نمی‌تواند خالی باشد.",
        )

    # Validate token via Telegram API
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        try:
            tg_resp = await http_client.get(f"https://api.telegram.org/bot{raw_token}/getMe")
            tg_data = tg_resp.json()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"خطا در برقراری ارتباط با سرور تلگرام: {str(e)}",
            )

    if tg_resp.status_code != 200 or not tg_data.get("ok"):
        err_desc = tg_data.get("description", "توکن نامعتبر است")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"توکن تلگرام نامعتبر است: {err_desc}",
        )

    tg_result = tg_data.get("result", {})
    bot_username = tg_result.get("username", "")
    bot_name = req.name.strip() if req.name and req.name.strip() else tg_result.get("first_name", bot_username)

    # Check for existing bot token
    existing = (await db.execute(select(TelegramBot).where(TelegramBot.token == raw_token))).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="این ربات قبلاً در سیستم ثبت شده است.",
        )

    # If first registered bot, automatically set active
    existing_count = (await db.execute(select(func.count(TelegramBot.id)))).scalar() or 0
    is_first = (existing_count == 0)

    new_bot = TelegramBot(
        id=uuid.uuid4(),
        name=bot_name,
        token=raw_token,
        username=bot_username,
        is_active=is_first,
    )
    db.add(new_bot)
    await db.commit()
    await db.refresh(new_bot)

    if is_first:
        settings.BOT_TOKEN = raw_token
        update_env_variable("BOT_TOKEN", raw_token)
        if bot_username:
            settings.BOT_USERNAME = bot_username
            update_env_variable("BOT_USERNAME", bot_username)

    if admin:
        await record_audit(
            db,
            action="ADD_TELEGRAM_BOT",
            details=f"Registered Telegram bot @{bot_username} ({new_bot.id})",
            actor_id=admin.telegram_id,
        )
        await db.commit()

    return {
        "success": True,
        "message": f"ربات @{bot_username} با موفقیت ثبت شد." + (" و به عنوان ربات فعال تنظیم گردید." if is_first else ""),
        "bot": {
            "id": str(new_bot.id),
            "name": new_bot.name,
            "token": new_bot.token,
            "username": new_bot.username,
            "is_active": new_bot.is_active,
            "created_at": new_bot.created_at.isoformat() if new_bot.created_at else None,
        },
    }


@router.post("/telegram-bots/{bot_id}/activate")
@router.post("/admin/telegram-bots/{bot_id}/activate")
async def activate_telegram_bot(
    bot_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set this bot as active, mark others inactive, and update settings.BOT_TOKEN."""
    try:
        b_uuid = uuid.UUID(str(bot_id))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ربات مورد نظر یافت نشد.",
        )

    target_bot = (await db.execute(select(TelegramBot).where(TelegramBot.id == b_uuid))).scalar_one_or_none()
    if not target_bot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ربات مورد نظر یافت نشد.",
        )

    # Deactivate all bots, activate the target bot
    all_bots = (await db.execute(select(TelegramBot))).scalars().all()
    for b in all_bots:
        b.is_active = (b.id == target_bot.id)

    # Update runtime settings and persist to .env
    settings.BOT_TOKEN = target_bot.token
    update_env_variable("BOT_TOKEN", target_bot.token)
    if target_bot.username:
        settings.BOT_USERNAME = target_bot.username
        update_env_variable("BOT_USERNAME", target_bot.username)

    if admin:
        await record_audit(
            db,
            action="ACTIVATE_TELEGRAM_BOT",
            details=f"Activated bot @{target_bot.username} ({target_bot.id})",
            actor_id=admin.telegram_id,
        )

    await db.commit()
    await db.refresh(target_bot)

    # Signal running bot process to reload token immediately
    try:
        Path("/tmp/bot_reload_signal").write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
    try:
        data_sig = Path("data/bot_reload_signal")
        data_sig.parent.mkdir(parents=True, exist_ok=True)
        data_sig.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass

    return {
        "success": True,
        "message": f"ربات @{target_bot.username} با موفقیت فعال شد.",
        "bot": {
            "id": str(target_bot.id),
            "name": target_bot.name,
            "token": target_bot.token,
            "username": target_bot.username,
            "is_active": True,
            "created_at": target_bot.created_at.isoformat() if target_bot.created_at else None,
        },
    }


@router.delete("/telegram-bots/{bot_id}")
@router.delete("/admin/telegram-bots/{bot_id}")
async def delete_telegram_bot(
    bot_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete Telegram bot from system."""
    try:
        b_uuid = uuid.UUID(str(bot_id))
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ربات مورد نظر یافت نشد.",
        )

    target_bot = (await db.execute(select(TelegramBot).where(TelegramBot.id == b_uuid))).scalar_one_or_none()
    if not target_bot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ربات مورد نظر یافت نشد.",
        )

    was_active = target_bot.is_active
    bot_username = target_bot.username
    await db.delete(target_bot)
    await db.commit()

    if was_active:
        next_bot = (await db.execute(select(TelegramBot).order_by(TelegramBot.created_at.desc()))).scalars().first()
        if next_bot:
            next_bot.is_active = True
            settings.BOT_TOKEN = next_bot.token
            update_env_variable("BOT_TOKEN", next_bot.token)
            if next_bot.username:
                settings.BOT_USERNAME = next_bot.username
                update_env_variable("BOT_USERNAME", next_bot.username)
            await db.commit()

    if admin:
        await record_audit(
            db,
            action="DELETE_TELEGRAM_BOT",
            details=f"Deleted Telegram bot @{bot_username} ({bot_id})",
            actor_id=admin.telegram_id,
        )
        await db.commit()

    return {
        "success": True,
        "message": "ربات با موفقیت حذف گردید.",
    }


@router.post("/telegram-bots/test-proxy")
@router.post("/admin/telegram-bots/test-proxy")
async def test_telegram_proxy_endpoint(
    req: TelegramBotProxyTestRequest,
    admin: User = Depends(get_current_admin),
):
    """Test connecting to api.telegram.org using the specified proxy URL."""
    proxy_url = req.proxy_url.strip()
    if not proxy_url:
        raise HTTPException(status_code=400, detail="آدرس پروکسی الزامی است.")

    import urllib.parse
    parsed = urllib.parse.urlparse(proxy_url)
    if parsed.scheme.lower() not in ("socks5", "socks5h", "socks4", "http", "https"):
        raise HTTPException(
            status_code=400,
            detail="پروتکل پروکسی نامعتبر است. فقط socks5, socks4, http, https پشتیبانی می‌شوند.",
        )

    import time
    import aiohttp
    from aiohttp_socks import ProxyConnector

    start_time = time.perf_counter()
    try:
        connector = ProxyConnector.from_url(proxy_url)
        timeout = aiohttp.ClientTimeout(total=10.0, connect=6.0)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get("https://api.telegram.org") as resp:
                latency_ms = int((time.perf_counter() - start_time) * 1000)
                status_code = resp.status
                return {
                    "success": True,
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                    "message": f"اتصال به سرورهای تلگرام موفقیت‌آمیز بود (تاخیر: {latency_ms} میلی‌ثانیه).",
                }
    except Exception as e:
        logger.warning(f"Telegram proxy test failed for {proxy_url}: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": f"خطا در اتصال به سرور تلگرام از طریق پروکسی: {str(e)}",
        }


@router.post("/telegram-bots/{bot_id}/test")
@router.post("/admin/telegram-bots/{bot_id}/test")
@router.post("/bot/{bot_id}/test")
@router.post("/bot/test-token")
async def test_telegram_bot_token(
    bot_id: Optional[str] = None,
    req: Optional[TelegramBotCreateRequest] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Test Telegram Bot API connection using getMe."""
    token_to_test = None
    if req and req.token:
        token_to_test = req.token.strip()
    elif bot_id:
        if bot_id in ("env", "default"):
            token_to_test = settings.BOT_TOKEN
        else:
            try:
                b_uuid = uuid.UUID(str(bot_id))
                bot_record = (await db.execute(select(TelegramBot).where(TelegramBot.id == b_uuid))).scalar_one_or_none()
                if bot_record:
                    token_to_test = bot_record.token
            except (ValueError, AttributeError):
                pass

    if not token_to_test:
        token_to_test = settings.BOT_TOKEN

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        try:
            tg_resp = await http_client.get(f"https://api.telegram.org/bot{token_to_test}/getMe")
            tg_data = tg_resp.json()
        except Exception as e:
            return {
                "success": False,
                "message": f"خطا در برقراری ارتباط با سرور تلگرام: {str(e)}",
            }

    if tg_resp.status_code == 200 and tg_data.get("ok"):
        res = tg_data.get("result", {})
        username = res.get("username", "")
        name = res.get("first_name", "")
        return {
            "success": True,
            "bot_username": username,
            "bot_name": name,
            "message": f"اتصال به ربات @{username} ({name}) با موفقیت تایید شد.",
        }
    else:
        err_desc = tg_data.get("description", "توکن نامعتبر است")
        return {
            "success": False,
            "message": f"خطا در اعتبارسنجی توکن: {err_desc}",
        }


@router.put("/telegram-bots/{bot_id}/proxy")
@router.put("/admin/telegram-bots/{bot_id}/proxy")
async def update_telegram_bot_proxy_endpoint(
    bot_id: str,
    req: TelegramBotProxyUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update proxy configuration for a telegram bot."""
    try:
        b_uuid = uuid.UUID(str(bot_id))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="ربات مورد نظر یافت نشد.")

    target_bot = (await db.execute(select(TelegramBot).where(TelegramBot.id == b_uuid))).scalar_one_or_none()
    if not target_bot:
        raise HTTPException(status_code=404, detail="ربات مورد نظر یافت نشد.")

    clean_url = (req.proxy_url or "").strip()
    target_bot.proxy_url = clean_url if clean_url else None
    target_bot.proxy_enabled = bool(req.proxy_enabled and clean_url)

    if target_bot.is_active:
        settings.TELEGRAM_PROXY = target_bot.proxy_url or ""
        settings.TELEGRAM_PROXY_ENABLED = target_bot.proxy_enabled
        update_env_variable("TELEGRAM_PROXY", target_bot.proxy_url or "")
        update_env_variable("TELEGRAM_PROXY_ENABLED", str(target_bot.proxy_enabled).lower())

    await db.commit()
    await db.refresh(target_bot)

    # Signal running bot process to reload token immediately
    try:
        Path("/tmp/bot_reload_signal").write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
    try:
        data_sig = Path("data/bot_reload_signal")
        data_sig.parent.mkdir(parents=True, exist_ok=True)
        data_sig.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass

    return {
        "success": True,
        "message": "تنظیمات پروکسی ربات با موفقیت ذخیره گردید.",
        "bot": {
            "id": str(target_bot.id),
            "name": target_bot.name,
            "username": target_bot.username,
            "proxy_url": target_bot.proxy_url,
            "proxy_enabled": target_bot.proxy_enabled,
            "is_active": target_bot.is_active,
        }
    }


@router.post("/logout")
async def admin_logout(
    request: Request,
    response: Response,
    auth_header: Optional[Any] = Depends(bearer_scheme),
):
    """Logout endpoint that revokes the token in blacklist and clears the cookie."""
    token = None
    if auth_header and hasattr(auth_header, "credentials") and auth_header.credentials:
        token = auth_header.credentials
    elif "admin_token" in request.cookies:
        token = request.cookies.get("admin_token")
    if token:
        await revoke_token(token)
    response.delete_cookie("admin_token")
    return {"success": True, "message": "با موفقیت خارج شدید."}


@router.get("/me")
async def get_current_admin_info(admin: User = Depends(get_current_admin)):
    return {
        "id": str(admin.id),
        "telegram_id": admin.telegram_id,
        "username": admin.username,
        "first_name": admin.first_name,
        "role": admin.role,
    }


# -------------------------------------------------------------
# 2. Dashboard Statistics & Overview
# -------------------------------------------------------------
@router.get("/stats")
async def get_dashboard_stats(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Fetch high-level metrics for the web dashboard."""
    # User counts
    users_count = (await db.execute(select(func.count(User.id)))).scalar() or 0
    
    # Subscriptions count
    active_subs = (await db.execute(select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.ACTIVE))).scalar() or 0
    total_subs = (await db.execute(select(func.count(Subscription.id)))).scalar() or 0

    # Pending Payments count
    pending_payments = (await db.execute(select(func.count(Payment.id)).where(Payment.status == PaymentStatus.PENDING))).scalar() or 0

    # Open Tickets count
    open_tickets = (await db.execute(
        select(func.count(SupportTicket.id)).where(SupportTicket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_CUSTOMER]))
    )).scalar() or 0

    # Total Sales (from paid payments)
    total_sales_irr = (await db.execute(
        select(func.sum(Payment.amount_irr)).where(Payment.status == PaymentStatus.PAID)
    )).scalar() or 0
    total_sales_toman = total_sales_irr // 10

    # Today's Sales
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_sales_irr = (await db.execute(
        select(func.sum(Payment.amount_irr)).where(and_(Payment.status == PaymentStatus.PAID, Payment.created_at >= today_start))
    )).scalar() or 0
    today_sales_toman = today_sales_irr // 10

    # Active Servers count
    active_servers = (await db.execute(select(func.count(Server.id)).where(Server.is_active == True))).scalar() or 0

    # Recent 5 Orders
    recent_orders_q = (
        select(Order)
        .options(selectinload(Order.user), selectinload(Order.product))
        .order_by(Order.created_at.desc())
        .limit(5)
    )
    recent_orders_res = (await db.execute(recent_orders_q)).scalars().all()
    recent_orders = [
        {
            "id": str(o.id),
            "customer": o.user.first_name if o.user else "کاربر",
            "telegram_id": o.user.telegram_id if o.user else None,
            "product": o.product.name if o.product else "سرویس",
            "amount_toman": o.total_price_toman,
            "status": o.status,
            "date": o.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for o in recent_orders_res
    ]

    return {
        "users_count": users_count,
        "active_subs": active_subs,
        "total_subs": total_subs,
        "pending_payments": pending_payments,
        "open_tickets": open_tickets,
        "total_sales_toman": total_sales_toman,
        "today_sales_toman": today_sales_toman,
        "active_servers": active_servers,
        "recent_orders": recent_orders,
    }


@router.get("/system/stats")
async def get_system_hardware_stats(
    admin: User = Depends(get_current_admin),
):
    """Retrieve realtime VPS hardware resource usage stats."""
    cpu_percent = 0.0
    ram_data = {"total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0, "percent": 0.0}
    disk_data = {"total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0, "percent": 0.0}
    uptime_hours = 0.0
    network_data = {"rx_mb": 0.0, "tx_mb": 0.0}

    if psutil:
        try:
            cpu_percent = float(psutil.cpu_percent(interval=None))
            vm = psutil.virtual_memory()
            ram_data = {
                "total_gb": round(vm.total / (1024**3), 2),
                "used_gb": round(vm.used / (1024**3), 2),
                "free_gb": round(vm.available / (1024**3), 2),
                "percent": float(vm.percent),
            }
            disk_path = os.path.splitdrive(os.getcwd())[0] + "\\" if os.name == "nt" else "/"
            du = psutil.disk_usage(disk_path)
            disk_data = {
                "total_gb": round(du.total / (1024**3), 2),
                "used_gb": round(du.used / (1024**3), 2),
                "free_gb": round(du.free / (1024**3), 2),
                "percent": float(du.percent),
            }
            uptime_sec = time.time() - psutil.boot_time()
            uptime_hours = round(uptime_sec / 3600.0, 2)

            net = psutil.net_io_counters()
            network_data = {
                "rx_mb": round(net.bytes_recv / (1024 * 1024), 2),
                "tx_mb": round(net.bytes_sent / (1024 * 1024), 2),
            }
        except Exception as e:
            logger.warning(f"psutil stats collection error: {e}")

    # Fallbacks if psutil was unavailable or values are empty
    if disk_data["total_gb"] == 0.0:
        try:
            root_path = os.path.splitdrive(os.getcwd())[0] + "\\" if os.name == "nt" else "/"
            du = shutil.disk_usage(root_path)
            disk_data = {
                "total_gb": round(du.total / (1024**3), 2),
                "used_gb": round(du.used / (1024**3), 2),
                "free_gb": round(du.free / (1024**3), 2),
                "percent": round((du.used / du.total) * 100, 1) if du.total else 0.0,
            }
        except Exception:
            pass

    return {
        "cpu_percent": cpu_percent,
        "ram": ram_data,
        "disk": disk_data,
        "uptime_hours": uptime_hours,
        "network": network_data,
    }


@router.get("/analytics/finance")
async def get_financial_analytics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate revenue, 14-day daily sales chart data, and product breakdown."""
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    fourteen_days_ago = today_start - timedelta(days=13)

    approved_statuses = [PaymentStatus.PAID, "PAID", "APPROVED"]

    # 1. Total revenue
    total_rev_irr = (
        await db.execute(
            select(func.sum(Payment.amount_irr)).where(Payment.status.in_(approved_statuses))
        )
    ).scalar() or 0
    total_revenue_toman = total_rev_irr // 10

    # 2. Today's revenue
    today_rev_irr = (
        await db.execute(
            select(func.sum(Payment.amount_irr)).where(
                and_(
                    Payment.status.in_(approved_statuses),
                    Payment.created_at >= today_start,
                )
            )
        )
    ).scalar() or 0
    today_revenue_toman = today_rev_irr // 10

    # 3. This month's revenue
    month_rev_irr = (
        await db.execute(
            select(func.sum(Payment.amount_irr)).where(
                and_(
                    Payment.status.in_(approved_statuses),
                    Payment.created_at >= month_start,
                )
            )
        )
    ).scalar() or 0
    month_revenue_toman = month_rev_irr // 10

    # 4. Last 14 days daily sales
    daily_map = {}
    for i in range(14):
        d_str = (fourteen_days_ago + timedelta(days=i)).strftime("%Y-%m-%d")
        daily_map[d_str] = {"date": d_str, "amount_toman": 0, "orders_count": 0}

    payments_14d = (
        await db.execute(
            select(Payment.amount_irr, Payment.created_at).where(
                and_(
                    Payment.status.in_(approved_statuses),
                    Payment.created_at >= fourteen_days_ago,
                )
            )
        )
    ).all()

    for p_amount, p_created in payments_14d:
        if p_created:
            d_key = p_created.strftime("%Y-%m-%d")
            if d_key in daily_map:
                daily_map[d_key]["amount_toman"] += int(p_amount // 10)
                daily_map[d_key]["orders_count"] += 1

    daily_sales = list(daily_map.values())

    # 5. Sales by plan / product breakdown
    prod_q = (
        select(
            Product.id,
            Product.name,
            func.count(Order.id).label("sales_count"),
            func.coalesce(func.sum(Order.total_price_irr), 0).label("total_irr"),
        )
        .join(Order, Product.id == Order.product_id)
        .join(Payment, Payment.order_id == Order.id)
        .where(Payment.status.in_(approved_statuses))
        .group_by(Product.id, Product.name)
        .order_by(desc("total_irr"))
    )
    prod_rows = (await db.execute(prod_q)).all()
    sales_by_product = [
        {
            "product_id": str(row.id),
            "product_name": row.name,
            "sales_count": row.sales_count,
            "revenue_toman": int(row.total_irr // 10),
        }
        for row in prod_rows
    ]

    return {
        "total_revenue_toman": total_revenue_toman,
        "today_revenue_toman": today_revenue_toman,
        "month_revenue_toman": month_revenue_toman,
        "daily_sales": daily_sales,
        "sales_by_product": sales_by_product,
    }



# -------------------------------------------------------------
# 3. Users Management
# -------------------------------------------------------------
@router.get("/users")
async def list_users(
    q: Optional[str] = Query(None, description="Search term (id, username, name)"),
    role: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(User).options(selectinload(User.wallet), selectinload(User.subscriptions))

    if q:
        q_clean = q.strip()
        if q_clean.isdigit():
            query = query.where(User.telegram_id == int(q_clean))
        else:
            term = f"%{q_clean}%"
            query = query.where(
                or_(
                    User.username.ilike(term),
                    User.first_name.ilike(term),
                    User.last_name.ilike(term),
                )
            )

    if role:
        query = query.where(User.role == role)

    total_count = (await db.execute(select(func.count(User.id)))).scalar() or 0
    query = query.order_by(User.created_at.desc()).offset(offset).limit(limit)
    users = (await db.execute(query)).scalars().all()

    items = []
    for u in users:
        items.append({
            "id": str(u.id),
            "telegram_id": u.telegram_id,
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "role": u.role,
            "is_banned": bool(u.is_banned),
            "balance_toman": u.wallet.balance_toman if u.wallet else 0,
            "services_count": len(u.subscriptions) if u.subscriptions else 0,
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "—",
        })

    return {"total": total_count, "items": items}


@router.post("/users/{user_id}/adjust_balance")
async def adjust_user_balance(
    user_id: uuid.UUID,
    req: AdjustBalanceRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")

    amount_toman = req.amount_toman
    if amount_toman == 0:
        raise HTTPException(status_code=400, detail="مبلغ تغییر موجودی نمی‌تواند صفر باشد.")

    amount_irr = abs(amount_toman) * 10
    wallet_svc = WalletService(db)

    try:
        if amount_toman < 0:
            new_balance = await wallet_svc.debit(
                user_id=user.id,
                amount_irr=amount_irr,
                order_id=f"WEB_DEBIT_{uuid.uuid4().hex[:8]}",
                reason="WEB_ADMIN_ADJUSTMENT",
            )
            action_msg = f"مبلغ {abs(amount_toman):,} تومان از موجودی شما کسر شد."
        else:
            new_balance = await wallet_svc.credit(
                user_id=user.id,
                amount_irr=amount_irr,
                payment_id=f"WEB_CREDIT_{uuid.uuid4().hex[:8]}",
                reason="WEB_ADMIN_TOPUP",
            )
            action_msg = f"مبلغ {amount_toman:,} تومان به موجودی کیف پول شما افزوده شد."
    except InsufficientFundsError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await db.commit()

    # Notify customer via Telegram
    bot = get_telegram_bot()
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"💳 <b>بروزرسانی موجودی کیف پول:</b>\n\n"
                f"{action_msg}\n"
                f"💰 موجودی فعلی شما: <b>{new_balance // 10:,} تومان</b>"
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not notify user {user.telegram_id} on telegram: {e}")
    finally:
        await bot.session.close()

    return {
        "success": True,
        "new_balance_toman": new_balance // 10,
        "message": f"موجودی با موفقیت ثبت شد. موجودی جدید: {new_balance // 10:,} تومان",
    }


@router.post("/users/{user_id}/role")
async def update_user_role(
    user_id: uuid.UUID,
    req: UpdateUserRoleRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")

    user.role = req.role
    await db.commit()
    return {"success": True, "role": user.role}


@router.post("/users/{user_id}/toggle-ban")
async def toggle_user_ban(
    user_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle ban/unban status of a user."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")

    user.is_banned = not bool(user.is_banned)
    await record_audit(
        db,
        action="USER_BAN_TOGGLED",
        details=f"User {user.telegram_id} ban status changed to {user.is_banned}",
        actor_id=admin.telegram_id,
        target_type="User",
        target_id=str(user.id),
    )
    await db.commit()
    return {
        "success": True,
        "is_banned": user.is_banned,
        "message": f"وضعیت کاربر با موفقیت به {'مسدود' if user.is_banned else 'فعال'} تغییر یافت.",
    }


@router.post("/users/{user_id}/send-message")
@router.post("/users/{user_id}/message")
async def send_direct_message_to_user(
    user_id: uuid.UUID,
    req: DirectMessageRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Send private Telegram message to a specific customer user."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر یافت نشد.")

    if not user.telegram_id:
        raise HTTPException(status_code=400, detail="این کاربر شناسه تلگرام معتبری ندارد یا ربات را استارت نکرده است.")

    msg_text = req.message.strip()
    if not msg_text:
        raise HTTPException(status_code=400, detail="متن پیام نمی‌تواند خالی باشد.")

    bot = get_telegram_bot()
    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=msg_text,
            parse_mode="HTML",
        )
    except Exception as exc:
        err_msg = str(exc)
        logger.warning(f"Failed to send direct message to user {user.telegram_id}: {exc}")
        if "blocked" in err_msg.lower():
            raise HTTPException(status_code=400, detail="ارسال پیام ناموفق بود: کاربر ربات را مسدود (بلاک) کرده است.")
        elif "chat not found" in err_msg.lower():
            raise HTTPException(status_code=400, detail="ارسال پیام ناموفق بود: چت تلگرام کاربر یافت نشد یا کاربر ربات را شروع نکرده است.")
        else:
            raise HTTPException(status_code=400, detail=f"خطا در ارسال پیام به تلگرام کاربر: {err_msg}")
    finally:
        await bot.session.close()

    return {
        "success": True,
        "message": "پیام خصوصی با موفقیت به کاربر ارسال شد.",
    }


# -------------------------------------------------------------
# 4. Payments and Order Receipts Management
# -------------------------------------------------------------
@router.get("/payments")
async def list_payments(
    status_filter: Optional[str] = Query(None, description="PENDING, PAID, REJECTED"),
    limit: int = 500,
    offset: int = 0,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Payment)
        .options(
            selectinload(Payment.user),
            selectinload(Payment.order).selectinload(Order.product),
            selectinload(Payment.receipt),
        )
    )

    if status_filter:
        query = query.where(Payment.status == status_filter.upper())

    total = (await db.execute(select(func.count(Payment.id)))).scalar() or 0
    query = query.order_by(Payment.created_at.desc()).offset(offset).limit(limit)
    payments = (await db.execute(query)).scalars().all()

    items = []
    for p in payments:
        product_name = p.order.product.name if (p.order and p.order.product) else "شارژ مستقیم کیف پول"
        receipt_photo = p.receipt.telegram_file_id if p.receipt else None
        tx_ref = p.receipt.transaction_ref if p.receipt else (p.reference_id or "—")
        has_receipt = bool(p.receipt and (p.receipt.telegram_file_id or (p.receipt.transaction_ref and p.receipt.transaction_ref != "—")))

        items.append({
            "id": str(p.id),
            "order_id": str(p.order_id) if p.order_id else None,
            "customer_name": p.user.first_name if p.user else "ناشناس",
            "customer_username": p.user.username if p.user else None,
            "customer_telegram_id": p.user.telegram_id if p.user else None,
            "product_name": product_name,
            "amount_toman": p.amount_toman,
            "provider": p.provider,
            "status": p.status,
            "tracking_code": p.tracking_code,
            "transaction_ref": tx_ref,
            "receipt_photo": receipt_photo,
            "has_receipt": has_receipt,
            "created_at": p.created_at.strftime("%Y-%m-%d %H:%M") if p.created_at else "—",
        })

    return {"total": total, "items": items}


@router.get("/telegram-file/{file_id}")
async def get_telegram_file_proxy(
    file_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Proxy or stream Telegram file download for viewing receipts and ticket attachments in Web Admin."""
    from app.database.models.bot import TelegramBot
    bot_token = settings.BOT_TOKEN
    active_bot = (await db.execute(select(TelegramBot).where(TelegramBot.is_active == True))).scalars().first()
    if active_bot and active_bot.token:
        bot_token = active_bot.token

    if not bot_token:
        raise HTTPException(status_code=400, detail="توکن ربات تلگرام تنظیم نشده است.")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            info_res = await client.get(
                f"https://api.telegram.org/bot{bot_token}/getFile",
                params={"file_id": file_id},
            )
            data = info_res.json()
            if not data.get("ok"):
                raise HTTPException(status_code=404, detail="فایل در تلگرام یافت نشد یا منقضی شده است.")
            file_path = data["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

            file_res = await client.get(download_url)
            content_type = file_res.headers.get("content-type", "image/jpeg")
            return Response(content=file_res.content, media_type=content_type)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error fetching telegram file {file_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"خطا در دریافت فایل از تلگرام: {exc}")


@router.post("/payments/{payment_id}/approve")
async def approve_payment(
    payment_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Approve a payment receipt, provision VPN on 3x-ui, and deliver subscription via Telegram."""
    order_svc = OrderService(db)
    sub_svc = SubscriptionService(db)

    payment = await db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="پرداخت یافت نشد.")

    if payment.status == PaymentStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="این پرداخت قبلاً تایید شده و سرویس کاربر فعال است.",
        )

    # Validate that only manual card-to-card or crypto payments with proof can be approved manually
    if payment.provider not in (PaymentMethod.CARD_TO_CARD, PaymentMethod.CRYPTO, "card_to_card", "crypto"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="فقط پرداخت‌های دستی (کارت‌به‌کارت و کریپتو) در این بخش قابل تایید هستند. پرداخت‌های آنلاین به صورت خودکار پردازش می‌شوند.",
        )

    receipt_res = await db.execute(select(PaymentReceipt).where(PaymentReceipt.payment_id == payment.id))
    receipt = receipt_res.scalars().first()
    if not receipt and not payment.reference_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="برای این سفارش هنوز فیش یا اطلاعات واریزی توسط مشتری ثبت نشده است و امکان تایید وجود ندارد.",
        )

    try:
        subscription = await order_svc.approve_and_provision_order(
            payment_id=payment_id,
            admin_id=admin.telegram_id,
            reference_id="APPROVED_VIA_WEB_ADMIN",
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error(f"Error approving payment {payment_id} via Web Admin: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"خطا در ایجاد سرویس در پنل: {exc}")

    # Deliver to customer on Telegram
    bot = get_telegram_bot()
    try:
        customer = await db.get(User, payment.user_id)
        if subscription and customer:
            conn = await sub_svc.get_connection_details(subscription.id)
            qr_buf = generate_qr_buffer(conn["subscription_url"])
            qr_file = BufferedInputFile(qr_buf.getvalue(), filename="sub_qr.png")

            unlimited_hint = (
                "\n\n💡 <i>این سرویس نامحدود چندلوکیشن است. با وارد کردن لینک اشتراک بالا در نرم‌افزار، تمامی لوکیشن‌های فعال به صورت خودکار بارگذاری می‌شوند.</i>\n"
                if subscription.traffic_limit_bytes == 0
                else ""
            )
            from app.modules.bot_texts.service import get_bot_text
            delivery_template = get_bot_text("order_approved_delivery")
            support_u = getattr(settings, "SUPPORT_USERNAME", "VPNPrivateSupport").lstrip("@")

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
            if unlimited_hint:
                customer_msg += unlimited_hint
            await bot.send_photo(
                chat_id=customer.telegram_id,
                photo=qr_file,
                caption=customer_msg,
                parse_mode="HTML",
            )
        elif customer:
            await bot.send_message(
                chat_id=customer.telegram_id,
                text=f"🎉 <b>شارژ حساب شما تایید شد!</b>\n\nمبلغ <b>{payment.amount_toman:,} تومان</b> به کیف پول شما افزوده گردید.",
                parse_mode="HTML",
            )
    except Exception as e_deliv:
        logger.warning(f"Failed to deliver approved sub to customer in Telegram: {e_deliv}")
    finally:
        await bot.session.close()

    return {"success": True, "message": "پرداخت با موفقیت تایید و سرویس کاربر تحویل داده شد."}


@router.post("/payments/{payment_id}/reject")
async def reject_payment(
    payment_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    payment = await db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="پرداخت یافت نشد.")

    if payment.status == PaymentStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="این پرداخت قبلاً تایید شده و سرویس کاربر فعال گردیده است. امکان رد پرداخت تاییدشده وجود ندارد.",
        )

    payment.status = PaymentStatus.REJECTED
    payment.admin_id = admin.telegram_id
    await db.commit()

    bot = get_telegram_bot()
    try:
        customer = await db.get(User, payment.user_id)
        if customer:
            await bot.send_message(
                chat_id=customer.telegram_id,
                text="❌ <b>متأسفانه واریزی شما تایید نشد.</b>\n\nدر صورت کسر وجه از حسابتان، لطفاً به بخش پشتیبانی تیکت ارسال فرمایید.",
                parse_mode="HTML",
            )
    except Exception:
        pass
    finally:
        await bot.session.close()

    return {"success": True, "message": "پرداخت با موفقیت رد شد."}


@router.delete("/payments/{payment_id}")
async def delete_payment(
    payment_id: uuid.UUID,
    delete_order_and_sub: bool = Query(True, description="Whether to also delete linked order and subscription"),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a payment record, deduct its amount from total sales/analytics, and clean up linked test orders/subscriptions."""
    payment = await db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="پرداخت مورد نظر یافت نشد.")

    amount_toman = payment.amount_toman
    tracking_code = payment.tracking_code or "بدون کد"
    order_id = payment.order_id

    # If linked to an order, clean up linked subscriptions and the order itself
    if order_id and delete_order_and_sub:
        order = await db.get(Order, order_id)
        if order:
            sub_res = await db.execute(select(Subscription).where(Subscription.order_id == order.id))
            linked_subs = sub_res.scalars().all()
            for sub in linked_subs:
                # Remove client from 3X-UI panel if exists
                if sub.panel_id:
                    panel = await db.get(Panel, sub.panel_id)
                    inbound = await db.get(Inbound, sub.inbound_id) if sub.inbound_id else None
                    if panel:
                        from app.integrations.xui.client import ThreeXUIClient
                        from app.integrations.xui.adapter import ThreeXUIPanelAdapter
                        client = ThreeXUIClient(
                            base_url=panel.base_url,
                            username=panel.username,
                            password=panel.password,
                            api_token=panel.api_token,
                            login_secret=panel.login_secret,
                        )
                        adapter = ThreeXUIPanelAdapter(client)
                        try:
                            remote_inb = inbound.remote_inbound_id if inbound else 1
                            await adapter.delete_client(remote_inb, sub.client_uuid, client_email=sub.client_email)
                        except Exception as e_del:
                            logger.warning(f"Could not delete client on panel during payment delete: {e_del}")
                        finally:
                            await client.close()

                # Decrement capacity counts
                if sub.server_id:
                    srv = await db.get(Server, sub.server_id)
                    if srv and srv.current_clients > 0:
                        srv.current_clients -= 1
                if sub.inbound_id:
                    inb = await db.get(Inbound, sub.inbound_id)
                    if inb and inb.client_count > 0:
                        inb.client_count -= 1

                await db.delete(sub)

            await db.delete(order)

    # Delete the payment record
    await db.delete(payment)
    await record_audit(
        db,
        action="PAYMENT_DELETED",
        details=f"Deleted payment {payment_id} ({amount_toman:,} Toman, Ref: {tracking_code}), Order: {order_id}",
        actor_id=admin.telegram_id,
        target_type="Payment",
        target_id=str(payment_id),
    )
    await db.commit()

    return {
        "success": True,
        "message": f"پرداخت با مبلغ {amount_toman:,} تومان با موفقیت حذف و از آمار فروش کسر شد.",
        "amount_toman": amount_toman,
    }


# -------------------------------------------------------------
# Multi-Card Banking System Endpoints
# -------------------------------------------------------------
@router.get("/payments/bank-cards")
async def list_bank_cards(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Returns list of all configured bank cards."""
    res = await db.execute(select(BankCard).order_by(BankCard.created_at.desc()))
    cards = res.scalars().all()
    return [
        {
            "id": str(c.id),
            "card_number": c.card_number,
            "card_holder": c.card_holder,
            "bank_name": c.bank_name,
            "iban": c.iban,
            "is_active": c.is_active,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in cards
    ]


@router.post("/payments/bank-cards")
async def create_bank_card(
    req: BankCardCreateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a new bank card for Card-to-Card payments."""
    card_num = req.card_number.strip().replace(" ", "").replace("-", "")
    if not card_num:
        raise HTTPException(status_code=400, detail="شماره کارت نمی‌تواند خالی باشد.")
    card_holder = req.card_holder.strip()
    if not card_holder:
        raise HTTPException(status_code=400, detail="نام دارنده کارت نمی‌تواند خالی باشد.")
    bank_name = req.bank_name.strip()
    if not bank_name:
        raise HTTPException(status_code=400, detail="نام بانک نمی‌تواند خالی باشد.")
    iban = req.iban.strip() if req.iban else None

    card = BankCard(
        id=uuid.uuid4(),
        card_number=card_num,
        card_holder=card_holder,
        bank_name=bank_name,
        iban=iban,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    await record_audit(
        db,
        action="BANK_CARD_CREATED",
        details=f"Added bank card {card.card_number} ({card.bank_name} - {card.card_holder})",
        actor_id=admin.telegram_id,
        target_id=str(card.id),
        target_type="BankCard",
    )
    await db.commit()
    await db.refresh(card)
    return {
        "success": True,
        "message": "کارت بانکی جدید با موفقیت اضافه شد.",
        "card": {
            "id": str(card.id),
            "card_number": card.card_number,
            "card_holder": card.card_holder,
            "bank_name": card.bank_name,
            "iban": card.iban,
            "is_active": card.is_active,
            "created_at": card.created_at.isoformat() if card.created_at else None,
        },
    }


@router.post("/payments/bank-cards/{card_id}/toggle")
async def toggle_bank_card(
    card_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle active state of a bank card."""
    card = await db.get(BankCard, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="کارت بانکی مورد نظر یافت نشد.")

    card.is_active = not card.is_active
    await record_audit(
        db,
        action="BANK_CARD_TOGGLED",
        details=f"Toggled bank card {card.card_number} active state to {card.is_active}",
        actor_id=admin.telegram_id,
        target_id=str(card.id),
        target_type="BankCard",
    )
    await db.commit()
    await db.refresh(card)
    status_str = "فعال" if card.is_active else "غیرفعال"
    return {
        "success": True,
        "message": f"وضعیت کارت با موفقیت به {status_str} تغییر یافت.",
        "is_active": card.is_active,
        "card": {
            "id": str(card.id),
            "card_number": card.card_number,
            "card_holder": card.card_holder,
            "bank_name": card.bank_name,
            "iban": card.iban,
            "is_active": card.is_active,
            "created_at": card.created_at.isoformat() if card.created_at else None,
        },
    }


@router.delete("/payments/bank-cards/{card_id}")
async def delete_bank_card(
    card_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a bank card."""
    card = await db.get(BankCard, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="کارت بانکی مورد نظر یافت نشد.")

    await record_audit(
        db,
        action="BANK_CARD_DELETED",
        details=f"Deleted bank card {card.card_number} ({card.bank_name})",
        actor_id=admin.telegram_id,
        target_id=str(card.id),
        target_type="BankCard",
    )
    await db.delete(card)
    await db.commit()
    return {"success": True, "message": "کارت بانکی با موفقیت حذف شد."}


# -------------------------------------------------------------
# Multi-Gateway Payment System Endpoints (SEP, IDPay, ZarinPal, Crypto)
# -------------------------------------------------------------
@router.get("/payments/gateways")
async def list_payment_gateways(
    provider: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all payment gateways, optionally filtered by provider."""
    query = select(PaymentGateway)
    if provider:
        query = query.where(PaymentGateway.provider == provider.lower())
    query = query.order_by(PaymentGateway.sort_order.asc(), PaymentGateway.created_at.desc())
    res = await db.execute(query)
    gateways = res.scalars().all()
    return [
        {
            "id": str(g.id),
            "provider": g.provider,
            "title": g.title,
            "is_active": g.is_active,
            "config": g.config_dict,
            "sort_order": g.sort_order,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "updated_at": g.updated_at.isoformat() if g.updated_at else None,
        }
        for g in gateways
    ]


@router.post("/payments/gateways")
async def create_payment_gateway(
    req: PaymentGatewayCreateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a new payment gateway instance (SEP, IDPay, Zarinpal, Crypto, etc.)."""
    prov = req.provider.strip().lower()
    if not prov:
        raise HTTPException(status_code=400, detail="نوع درگاه نمی‌تواند خالی باشد.")
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="عنوان درگاه نمی‌تواند خالی باشد.")

    gw = PaymentGateway(
        id=uuid.uuid4(),
        provider=prov,
        title=title,
        is_active=req.is_active,
        sort_order=req.sort_order,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    gw.set_config_dict(req.config)
    db.add(gw)
    await record_audit(
        db,
        action="PAYMENT_GATEWAY_CREATED",
        details=f"Added {prov} gateway '{title}'",
        actor_id=admin.telegram_id,
        target_id=str(gw.id),
        target_type="PaymentGateway",
    )
    await db.commit()
    await db.refresh(gw)
    return {
        "success": True,
        "message": f"درگاه «{title}» با موفقیت افزوده شد.",
        "gateway": {
            "id": str(gw.id),
            "provider": gw.provider,
            "title": gw.title,
            "is_active": gw.is_active,
            "config": gw.config_dict,
            "sort_order": gw.sort_order,
            "created_at": gw.created_at.isoformat() if gw.created_at else None,
        },
    }


@router.put("/payments/gateways/{gw_id}")
async def update_payment_gateway(
    gw_id: uuid.UUID,
    req: PaymentGatewayUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update gateway title, configuration, active state, or sort order."""
    gw = await db.get(PaymentGateway, gw_id)
    if not gw:
        raise HTTPException(status_code=404, detail="درگاه مورد نظر یافت نشد.")

    if req.title is not None:
        t = req.title.strip()
        if not t:
            raise HTTPException(status_code=400, detail="عنوان درگاه نمی‌تواند خالی باشد.")
        gw.title = t

    if req.config is not None:
        gw.set_config_dict(req.config)

    if req.is_active is not None:
        gw.is_active = req.is_active

    if req.sort_order is not None:
        gw.sort_order = req.sort_order

    gw.updated_at = datetime.now(timezone.utc)

    await record_audit(
        db,
        action="PAYMENT_GATEWAY_UPDATED",
        details=f"Updated gateway '{gw.title}' ({gw.provider})",
        actor_id=admin.telegram_id,
        target_id=str(gw.id),
        target_type="PaymentGateway",
    )
    await db.commit()
    await db.refresh(gw)
    return {
        "success": True,
        "message": "درگاه با موفقیت بروزرسانی شد.",
        "gateway": {
            "id": str(gw.id),
            "provider": gw.provider,
            "title": gw.title,
            "is_active": gw.is_active,
            "config": gw.config_dict,
            "sort_order": gw.sort_order,
            "created_at": gw.created_at.isoformat() if gw.created_at else None,
            "updated_at": gw.updated_at.isoformat() if gw.updated_at else None,
        },
    }


@router.post("/payments/gateways/{gw_id}/toggle")
async def toggle_payment_gateway(
    gw_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle active state of a payment gateway."""
    gw = await db.get(PaymentGateway, gw_id)
    if not gw:
        raise HTTPException(status_code=404, detail="درگاه مورد نظر یافت نشد.")

    gw.is_active = not gw.is_active
    gw.updated_at = datetime.now(timezone.utc)

    await record_audit(
        db,
        action="PAYMENT_GATEWAY_TOGGLED",
        details=f"Toggled gateway '{gw.title}' ({gw.provider}) to {gw.is_active}",
        actor_id=admin.telegram_id,
        target_id=str(gw.id),
        target_type="PaymentGateway",
    )
    await db.commit()
    await db.refresh(gw)
    status_str = "فعال" if gw.is_active else "غیرفعال"
    return {
        "success": True,
        "message": f"وضعیت درگاه «{gw.title}» به {status_str} تغییر یافت.",
        "is_active": gw.is_active,
    }


@router.delete("/payments/gateways/{gw_id}")
async def delete_payment_gateway(
    gw_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a payment gateway instance."""
    gw = await db.get(PaymentGateway, gw_id)
    if not gw:
        raise HTTPException(status_code=404, detail="درگاه مورد نظر یافت نشد.")

    title = gw.title
    prov = gw.provider
    await record_audit(
        db,
        action="PAYMENT_GATEWAY_DELETED",
        details=f"Deleted gateway '{title}' ({prov})",
        actor_id=admin.telegram_id,
        target_id=str(gw.id),
        target_type="PaymentGateway",
    )
    await db.delete(gw)
    await db.commit()
    return {"success": True, "message": f"درگاه «{title}» با موفقیت حذف شد."}



@router.delete("/orders/{order_id}")
async def delete_order(
    order_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete an order, any linked payments (deducting from sales), and linked subscriptions."""
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="سفارش مورد نظر یافت نشد.")

    order_toman = order.total_price_toman

    # Find and delete payments linked to this order
    pay_res = await db.execute(select(Payment).where(Payment.order_id == order.id))
    for p in pay_res.scalars().all():
        await db.delete(p)

    # Find and delete subscriptions linked to this order
    sub_res = await db.execute(select(Subscription).where(Subscription.order_id == order.id))
    for sub in sub_res.scalars().all():
        if sub.panel_id:
            panel = await db.get(Panel, sub.panel_id)
            inbound = await db.get(Inbound, sub.inbound_id) if sub.inbound_id else None
            if panel:
                from app.integrations.xui.client import ThreeXUIClient
                from app.integrations.xui.adapter import ThreeXUIPanelAdapter
                client = ThreeXUIClient(
                    base_url=panel.base_url,
                    username=panel.username,
                    password=panel.password,
                    api_token=panel.api_token,
                    login_secret=panel.login_secret,
                )
                adapter = ThreeXUIPanelAdapter(client)
                try:
                    remote_inb = inbound.remote_inbound_id if inbound else 1
                    await adapter.delete_client(remote_inb, sub.client_uuid, client_email=sub.client_email)
                except Exception:
                    pass
                finally:
                    await client.close()

        if sub.server_id:
            srv = await db.get(Server, sub.server_id)
            if srv and srv.current_clients > 0:
                srv.current_clients -= 1
        if sub.inbound_id:
            inb = await db.get(Inbound, sub.inbound_id)
            if inb and inb.client_count > 0:
                inb.client_count -= 1

        await db.delete(sub)

    await db.delete(order)
    await record_audit(
        db,
        action="ORDER_DELETED",
        details=f"Deleted order {order_id} ({order_toman:,} Toman)",
        actor_id=admin.telegram_id,
        target_type="Order",
        target_id=str(order_id),
    )
    await db.commit()

    return {
        "success": True,
        "message": f"سفارش با مبلغ {order_toman:,} تومان با موفقیت حذف و از آمار فروش کسر گردید.",
    }


# -------------------------------------------------------------
# 5. VPN Subscriptions Management
# -------------------------------------------------------------
@router.get("/subscriptions")
async def list_subscriptions(
    limit: int = 500,
    offset: int = 0,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Subscription)
        .options(
            selectinload(Subscription.user),
            selectinload(Subscription.server),
            selectinload(Subscription.order).selectinload(Order.product),
        )
        .order_by(Subscription.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    subs = (await db.execute(query)).scalars().all()
    total = (await db.execute(select(func.count(Subscription.id)))).scalar() or 0

    LOCATION_MAP = {
        "DE": {"code": "DE", "name": "آلمان", "flag": "🇩🇪"},
        "NL": {"code": "NL", "name": "هلند", "flag": "🇳🇱"},
        "UK": {"code": "UK", "name": "انگلیس", "flag": "🇬🇧"},
        "US": {"code": "US", "name": "آمریکا", "flag": "🇺🇸"},
        "FI": {"code": "FI", "name": "فنلاند", "flag": "🇫🇮"},
        "NO": {"code": "NO", "name": "نروژ", "flag": "🇳🇴"},
        "FR": {"code": "FR", "name": "فرانسه", "flag": "🇫🇷"},
        "IT": {"code": "IT", "name": "ایتالیا", "flag": "🇮🇹"},
        "PL": {"code": "PL", "name": "لهستان", "flag": "🇵🇱"},
        "DK": {"code": "DK", "name": "دانمارک", "flag": "🇩🇰"},
        "AZ": {"code": "az", "name": "قبرس", "flag": "🇨🇾"},
        "az": {"code": "az", "name": "قبرس", "flag": "🇨🇾"},
        "YRM": {"code": "YRM", "name": "ارمنستان", "flag": "🇦🇲"},
        "EG": {"code": "EG", "name": "مصر", "flag": "🇪🇬"},
        "GL": {"code": "GL", "name": "گرینلند", "flag": "🇬🇱"},
        "GH": {"code": "GH", "name": "غنا", "flag": "🇬🇭"},
        "AR": {"code": "AR", "name": "امارات", "flag": "🇦🇪"},
        "ALL": {"code": "ALL", "name": "تمام لوکیشن‌ها", "flag": "🌐"},
    }
    PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

    # Build dynamic map of active server locations so custom/synced inbounds display their real name and flag
    all_srvs = (await db.execute(select(Server))).scalars().all()
    for srv in all_srvs:
        if srv.location_code:
            c_key = srv.location_code.strip()
            LOCATION_MAP[c_key] = {
                "code": c_key,
                "name": srv.location_name or srv.name or c_key,
                "flag": srv.flag_emoji or "🌐",
            }
            LOCATION_MAP[c_key.upper()] = LOCATION_MAP[c_key]

    sub_svc = SubscriptionService(db)
    items = []
    for s in subs:
        is_unlimited = (s.traffic_limit_bytes <= 0)
        used_gb = round(s.traffic_used_bytes / (1024**3), 2)
        total_gb = 0.0 if is_unlimited else round(s.traffic_limit_bytes / (1024**3), 2)
        rem_gb = None if is_unlimited else max(0.0, round(total_gb - used_gb, 2))

        product = s.order.product if (s.order and s.order.product) else None

        if product:
            plan_name = product.name
        elif s.traffic_limit_bytes == 1073741824:
            plan_name = "تست رایگان ۱ گیگ"
        elif s.traffic_limit_bytes <= 0:
            plan_name = "نامحدود"
        else:
            plan_name = "پلن حجمی"

        is_trial = (s.traffic_limit_bytes == 1073741824) or ("تست" in plan_name)
        explicit_sub_locs = getattr(s, "allowed_locations", None)
        allowed_locations_raw = (
            explicit_sub_locs
            if (explicit_sub_locs and explicit_sub_locs.strip())
            else (
                product.allowed_locations
                if (product and product.allowed_locations)
                else (s.server.location_code if (s.server and s.server.location_code) else "ALL")
            )
        )

        if is_trial or (product is None and not explicit_sub_locs and s.server):
            server_loc_name = s.server.location_name if (s.server and s.server.location_name) else (s.server.name if s.server else "آلمان ۱")
            location_display = f"تست رایگان ({server_loc_name})" if is_trial else server_loc_name
            locations_count = 1
            is_multi_location = False
            locations = [{
                "code": s.server.location_code if s.server else "DE",
                "name": server_loc_name,
                "flag": s.server.flag_emoji if s.server else "🌐",
            }]
        elif allowed_locations_raw.strip().upper() == "ALL":
            locations_count = 15
            is_multi_location = True
            locations = [{"code": "ALL", "name": "تمام لوکیشن‌ها", "flag": "🌐"}]
            if is_unlimited:
                location_display = "نامحدود (تمام لوکیشن‌ها)"
            else:
                location_display = "حجمی (تمام لوکیشن‌ها)"
        else:
            raw_codes = [c.strip() for c in allowed_locations_raw.split(",") if c.strip()]
            locations = []
            seen_codes = set()
            for code in raw_codes:
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                loc_info = LOCATION_MAP.get(code) or LOCATION_MAP.get(code.upper())
                if loc_info:
                    locations.append(loc_info)
                else:
                    locations.append({"code": code, "name": code, "flag": "🌐"})

            locations_count = len(locations)
            is_multi_location = (locations_count > 1)
            names_str = "، ".join([loc["name"] for loc in locations])
            count_fa = str(locations_count).translate(PERSIAN_DIGITS)

            if is_unlimited:
                if locations_count > 1:
                    location_display = f"نامحدود {count_fa} لوکیشن ({names_str})"
                else:
                    location_display = f"نامحدود ۱ لوکیشن ({names_str})"
            else:
                if locations_count > 1:
                    location_display = f"حجمی {count_fa} لوکیشن ({names_str})"
                else:
                    location_display = f"حجمی ({names_str})"

        items.append({
            "id": str(s.id),
            "customer_telegram_id": (s.user.telegram_id if (s.user and s.user.telegram_id) else "وب‌پنل"),
            "customer_name": (
                s.client_email.split("_")[0]
                if (s.user and s.user.telegram_id == 0 and s.client_email)
                else (s.user.first_name if s.user else "کاربر")
            ),
            "email": s.client_email,
            "protocol": s.protocol.upper() if s.protocol else "VLESS",
            "location": s.server.location_name if s.server else "پیش‌فرض",
            "flag": s.server.flag_emoji if s.server else "🌐",
            "used_gb": used_gb,
            "total_gb": total_gb,
            "remaining_gb": rem_gb,
            "is_unlimited": is_unlimited,
            "status": s.status,
            "expires_at": s.expires_at.strftime("%Y-%m-%d %H:%M") if s.expires_at else "نامحدود",
            "plan_name": plan_name,
            "locations_count": locations_count,
            "is_multi_location": is_multi_location,
            "location_display": location_display,
            "locations": locations,
        })

    return {"total": total, "items": items}


@router.post("/subscriptions/quick-create")
async def quick_create_subscription(
    req: QuickCreateSubscriptionRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Quick / Manual Config Generation for VIPs, friends, or manual sales.
    Provisions a new client directly on 3X-UI panel and records Subscription in DB.
    """
    if req.traffic_gb < 0:
        raise HTTPException(status_code=400, detail="حجم ترافیک نمی‌تواند منفی باشد.")
    if req.duration_days < 0:
        raise HTTPException(status_code=400, detail="مدت زمان اشتراک نمی‌تواند منفی باشد.")

    from app.core.constants import ProtocolType
    valid_protocols = {p.value for p in ProtocolType} | {"wg", "awg", "ovpn"}
    req_proto = (req.protocol or "vless").strip().lower()
    if req_proto not in valid_protocols:
        raise HTTPException(
            status_code=400,
            detail=f"پروتکل انتخاب شده نامعتبر است ({req_proto}). پروتکل‌های مجاز: {', '.join(sorted(valid_protocols))}",
        )

    # 1. Resolve Target User (if user_id is omitted, bind to a dedicated Manual Clients account so it doesn't clutter admin's personal Telegram bot services)
    target_user = None
    if req.user_id and str(req.user_id).strip():
        uid_str = str(req.user_id).strip().lstrip("@")
        try:
            u_uuid = uuid.UUID(uid_str)
            target_user = await db.get(User, u_uuid)
        except ValueError:
            pass

        if not target_user and uid_str.isdigit():
            target_user = (await db.execute(select(User).where(User.telegram_id == int(uid_str)))).scalars().first()
            if not target_user:
                target_user = User(
                    id=uuid.uuid4(),
                    telegram_id=int(uid_str),
                    username=None,
                    first_name=(req.remark.strip() if req.remark else f"کاربر {uid_str}"),
                    role=UserRole.CUSTOMER,
                    is_active=True,
                    is_banned=False,
                    referral_code=f"REF_{uid_str}_{secrets.token_hex(2).upper()}",
                )
                db.add(target_user)
                await db.flush()
        if not target_user:
            target_user = (await db.execute(select(User).where(func.lower(User.username) == uid_str.lower()))).scalars().first()

        if not target_user:
            raise HTTPException(status_code=404, detail="کاربر با این یوزرنیم یافت نشد. لطفاً آیدی عددی تلگرام مشتری را وارد نمایید.")
    else:
        manual_holder = (await db.execute(select(User).where(User.telegram_id == 0))).scalars().first()
        if not manual_holder:
            manual_holder = User(
                id=uuid.uuid4(),
                telegram_id=0,
                username="manual_clients",
                first_name="مشتری دستی (وب‌پنل)",
                role=UserRole.CUSTOMER,
                is_active=True,
                is_banned=False,
                referral_code="MANUAL_WEB_CLIENTS",
            )
            db.add(manual_holder)
            await db.flush()
        target_user = manual_holder

    # 2. Resolve Target Server(s) (supports single server_id or multi-location server_ids)
    selected_servers: List[Server] = []
    raw_server_ids: List[str] = []
    if req.server_ids:
        raw_server_ids = [str(sid).strip() for sid in req.server_ids if sid and str(sid).strip()]
    elif req.server_id:
        raw_server_ids = [s.strip() for s in str(req.server_id).split(",") if s.strip()]

    for sid in raw_server_ids:
        try:
            s_uuid = uuid.UUID(sid)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="شناسه سرور ارسال‌شده نامعتبر است.")
        srv_obj = await db.get(Server, s_uuid)
        if not srv_obj:
            raise HTTPException(status_code=404, detail="سرور مشخص‌شده یافت نشد.")
        if srv_obj not in selected_servers:
            selected_servers.append(srv_obj)

    target_server = selected_servers[0] if selected_servers else None

    if not target_server or not target_server.is_active:
        srv_stmt = (
            select(Server)
            .where(Server.is_active == True)
            .order_by(Server.priority.desc(), Server.created_at.asc())
        )
        active_srv = (await db.execute(srv_stmt)).scalars().first()
        if active_srv:
            target_server = active_srv
            if not selected_servers:
                selected_servers = [target_server]

    # Self-healing fallback: If no active server exists yet, bind to any panel or create standalone server
    if not target_server:
        candidate_panel = (await db.execute(select(Panel).where(Panel.is_active == True))).scalars().first()
        if not candidate_panel:
            candidate_panel = (await db.execute(select(Panel))).scalars().first()

        if not candidate_panel:
            candidate_panel = Panel(
                id=uuid.uuid4(),
                name="پنل پیش‌فرض سیستم",
                base_url="http://127.0.0.1:2053",
                username="admin",
                password="default_admin_password",
                is_active=True,
                is_healthy=True,
                panel_type="3x-ui",
            )
            db.add(candidate_panel)
            await db.commit()
            await db.refresh(candidate_panel)

        import urllib.parse
        parsed_u = urllib.parse.urlparse(candidate_panel.base_url)
        sub_host = parsed_u.hostname or "127.0.0.1"
        target_server = Server(
            id=uuid.uuid4(),
            name=f"سرور {candidate_panel.name}",
            location_code="ALL",
            location_name=candidate_panel.name,
            flag_emoji="🌐",
            priority=10,
            panel_id=candidate_panel.id,
            subscription_host=sub_host,
            subscription_port=parsed_u.port or 443,
            is_active=True,
            capacity_limit=1000,
        )
        db.add(target_server)
        await db.commit()
        await db.refresh(target_server)
        if not selected_servers:
            selected_servers = [target_server]

    # 3. Resolve Target Panel & Inbound
    panel = None
    if target_server.panel_id:
        panel = await db.get(Panel, target_server.panel_id)

    req_proto = (req.protocol or "vless").strip().lower()

    # Find matching active inbound for target_server on this panel
    from app.modules.servers.allocation import inbound_matches_location

    def _matches_proto(inb_proto: str, target_proto: str) -> bool:
        ip = (inb_proto or "").strip().lower()
        tp = (target_proto or "").strip().lower()
        if ip == tp:
            return True
        if tp in ("amneziawg", "awg") and ip in ("amneziawg", "awg"):
            return True
        if tp in ("wireguard", "wg") and ip in ("wireguard", "wg"):
            return True
        if tp in ("openvpn", "ovpn") and ip in ("openvpn", "ovpn"):
            return True
        return False

    all_inbounds = []
    if panel and panel.is_active:
        all_inbounds_stmt = (
            select(Inbound)
            .where(
                and_(
                    Inbound.panel_id == panel.id,
                    Inbound.is_active == True,
                )
            )
            .order_by(Inbound.client_count.asc())
        )
        all_inbounds = (await db.execute(all_inbounds_stmt)).scalars().all()

    inbound = None
    # 0. Direct link via target_server.inbound_id (exact inbound chosen by admin)
    if getattr(target_server, "inbound_id", None):
        inbound = await db.get(Inbound, target_server.inbound_id)
        if inbound:
            req_proto = (inbound.protocol or req_proto).strip().lower()

    # 1. Match protocol and location
    proto_inbounds = [ib for ib in all_inbounds if _matches_proto(ib.protocol, req_proto)]
    if not inbound:
        for ib in proto_inbounds:
            if inbound_matches_location(ib.remark, ib.tag, target_server.location_code, ib.remote_inbound_id):
                inbound = ib
                break

    # 2. Match location across any protocol
    if not inbound:
        for ib in all_inbounds:
            if inbound_matches_location(ib.remark, ib.tag, target_server.location_code, ib.remote_inbound_id):
                inbound = ib
                req_proto = (inbound.protocol or req_proto).strip().lower()
                break

    # 3. Fallback to matching protocol
    if not inbound and proto_inbounds:
        inbound = proto_inbounds[0]

    # 4. Fallback to any active inbound
    if not inbound and all_inbounds:
        inbound = all_inbounds[0]

    # 5. Virtual inbound fallback if no panel inbounds exist
    if not inbound:
        inb_cand = (await db.execute(select(Inbound).where(Inbound.protocol == req_proto))).scalars().first()
        if inb_cand:
            inbound = inb_cand
        else:
            inbound = Inbound(
                id=uuid.uuid4(),
                panel_id=target_server.panel_id,
                remote_inbound_id=1,
                protocol=req_proto,
                port=443 if req_proto != "openvpn" else 1194,
                listen="0.0.0.0",
                tag=f"inbound-{req_proto}-direct",
                remark=f"Direct {req_proto.upper()}",
                stream_settings=json.dumps({
                    "network": "tcp",
                    "security": "reality" if req_proto == "vless" else "none",
                    "realitySettings": {
                        "serverNames": [target_server.subscription_host or "127.0.0.1"],
                        "publicKey": "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789+=",
                        "shortId": "12345678"
                    }
                }),
                is_active=True,
                client_count=0,
                max_clients=1000
            )
            db.add(inbound)
            await db.commit()
            await db.refresh(inbound)

    # Resolve exact selected inbound(s) for all selected_servers
    selected_inbounds: List[Inbound] = [inbound] if inbound else []
    selected_remote_ids: List[int] = [inbound.remote_inbound_id] if (inbound and inbound.remote_inbound_id) else []
    selected_loc_codes: List[str] = []
    for srv_item in selected_servers:
        if srv_item.location_code and srv_item.location_code not in selected_loc_codes:
            selected_loc_codes.append(srv_item.location_code)
        srv_inb = None
        if getattr(srv_item, "inbound_id", None):
            srv_inb = await db.get(Inbound, srv_item.inbound_id)
        if not srv_inb and all_inbounds:
            for ib in all_inbounds:
                if inbound_matches_location(ib.remark, ib.tag, srv_item.location_code, ib.remote_inbound_id):
                    srv_inb = ib
                    break
        if srv_inb:
            if srv_inb not in selected_inbounds:
                selected_inbounds.append(srv_inb)
            if srv_inb.remote_inbound_id and srv_inb.remote_inbound_id not in selected_remote_ids:
                selected_remote_ids.append(srv_inb.remote_inbound_id)

    if not selected_remote_ids and inbound and inbound.remote_inbound_id:
        selected_remote_ids = [inbound.remote_inbound_id]
    if not selected_loc_codes and target_server and target_server.location_code:
        selected_loc_codes = [target_server.location_code]

    # 4. Generate Identifiers & Client Email
    client_uuid = str(uuid.uuid4())
    subscription_token = secrets.token_hex(16)

    email_suffix = await SettingsService.get_effective_email_suffix(db)
    config_prefix = await SettingsService.get_effective_config_title_prefix(db)

    if req.remark:
        clean_remark = "".join(c for c in req.remark.strip() if c.isalnum() or c in ("-", "_")).strip("-_")
        if clean_remark:
            client_email = f"{clean_remark}_{secrets.token_hex(3)}{email_suffix}"
        else:
            client_email = f"custom_{secrets.token_hex(4)}{email_suffix}"
    else:
        client_email = f"custom_{secrets.token_hex(4)}{email_suffix}"

    # Ensure unique client_email in local database
    existing_sub = (await db.execute(select(Subscription).where(Subscription.client_email == client_email))).scalars().first()
    while existing_sub:
        client_email = f"custom_{secrets.token_hex(4)}{email_suffix}"
        existing_sub = (await db.execute(select(Subscription).where(Subscription.client_email == client_email))).scalars().first()

    # 5. Traffic & Expiry calculations
    traffic_limit_bytes = 0 if req.traffic_gb <= 0 else int(req.traffic_gb * (1024**3))
    now = datetime.now(timezone.utc)
    if req.duration_days > 0:
        expires_at = now + timedelta(days=req.duration_days)
        expiry_ms = int(expires_at.timestamp() * 1000)
    else:
        expires_at = None
        expiry_ms = 0

    # 6. Create ClientSpec (support Trojan password, VMess alterId, Shadowsocks method)
    spec = ClientSpec(
        id=client_uuid,
        email=client_email,
        flow="xtls-rprx-vision" if req_proto == "vless" else "",
        limitIp=2,
        totalGB=traffic_limit_bytes,
        expiryTime=expiry_ms,
        enable=True,
        tgId=int(target_user.telegram_id) if (target_user and target_user.telegram_id) else 0,
        subId=subscription_token,
        password=client_uuid,
        alterId=0,
        method="aes-128-gcm",
    )

    # 7. Connect to panel and provision client (if panel is active)
    adapter = None
    if panel and panel.is_active:
        from app.integrations.factory import get_panel_adapter
        from app.integrations.vpn_ui.adapter import VpnUIPanelAdapter
        from app.integrations.vpn_ui.schemas import WireGuardClientSpec, OpenVPNClientSpec

        try:
            adapter = get_panel_adapter(panel, timeout=10.0, verify_ssl=False)
            if req_proto in ("wireguard", "amneziawg") and isinstance(adapter, VpnUIPanelAdapter):
                wg_spec = WireGuardClientSpec(
                    id=client_uuid,
                    email=client_email,
                    totalGB=traffic_limit_bytes,
                    expiryTime=expiry_ms,
                    enable=True,
                    tgId=int(target_user.telegram_id) if (target_user and target_user.telegram_id) else 0,
                    subId=subscription_token,
                )
                if req_proto == "wireguard":
                    for rid in selected_remote_ids:
                        await adapter.add_wireguard_client(rid, wg_spec)
                else:
                    for rid in selected_remote_ids:
                        await adapter.add_amneziawg_client(rid, wg_spec)
            elif req_proto == "openvpn" and isinstance(adapter, VpnUIPanelAdapter):
                ovpn_spec = OpenVPNClientSpec(
                    id=client_uuid,
                    username=client_email.split("@")[0],
                    email=client_email,
                    totalGB=traffic_limit_bytes,
                    expiryTime=expiry_ms,
                    enable=True,
                    subId=subscription_token,
                )
                for rid in selected_remote_ids:
                    await adapter.add_openvpn_client(rid, ovpn_spec)
            else:
                if len(selected_remote_ids) > 1:
                    await adapter.add_clients_to_inbounds(selected_remote_ids, spec)
                else:
                    await adapter.add_client(inbound.remote_inbound_id, spec)
        except Exception as exc:
            logger.warning(f"Could not provision quick client on panel ({panel.name}): {exc}")

    # 8. Create Subscription in DB & Update Capacity Counters
    manual_allowed_locs = ",".join(selected_loc_codes) if selected_loc_codes else (target_server.location_code or "ALL")
    sub = Subscription(
        id=uuid.uuid4(),
        user_id=target_user.id,
        order_id=None,
        server_id=target_server.id,
        panel_id=panel.id if panel else None,
        inbound_id=inbound.id if inbound else None,
        client_uuid=client_uuid,
        client_email=client_email,
        protocol=req_proto,
        subscription_token=subscription_token,
        traffic_limit_bytes=traffic_limit_bytes,
        traffic_used_bytes=0,
        expires_at=expires_at,
        status=SubscriptionStatus.ACTIVE,
        allowed_locations=manual_allowed_locs,
    )
    db.add(sub)
    for sel_inb in selected_inbounds:
        sel_inb.client_count += 1
    for sel_srv in selected_servers:
        sel_srv.current_clients += 1

    # 9. Build Subscription URL and Direct Link
    sub_port = target_server.subscription_port or 443
    port_part = f":{sub_port}" if sub_port not in (80, 443) else ""
    sub_host = target_server.subscription_host or "127.0.0.1"
    sub_url = f"https://{sub_host}{port_part}/sub/{subscription_token}"

    direct_link = None
    raw_config = None
    try:
        inbound_stream = {}
        if inbound and inbound.stream_settings:
            try:
                inbound_stream = json.loads(inbound.stream_settings)
            except Exception:
                pass
        inbound_dict = {
            "port": inbound.port if inbound else 443,
            "remark": req.remark or f"{config_prefix}-{target_server.location_code}",
            "stream_settings": inbound_stream,
            "settings": inbound_stream,
            "protocol": inbound.protocol if inbound else req_proto,
        }
        client_dict = {
            "id": client_uuid,
            "email": client_email,
            "username": client_email.split("@")[0],
            "password": client_uuid,
            "remark": req.remark or f"{config_prefix}-{target_server.location_code}",
            "flow": "xtls-rprx-vision" if req_proto == "vless" else "",
        }

        if adapter:
            from app.integrations.vpn_ui.adapter import VpnUIPanelAdapter
            active_adapter = adapter
            if req_proto in ("wireguard", "amneziawg", "openvpn") and not isinstance(active_adapter, VpnUIPanelAdapter):
                active_adapter = VpnUIPanelAdapter(client=adapter.client)

            direct_link = active_adapter.generate_client_url(
                protocol=req_proto,
                inbound_data=inbound_dict,
                client_data=client_dict,
                server_host=sub_host,
            )

            if req_proto in ("wireguard", "amneziawg", "openvpn"):
                if req_proto == "wireguard":
                    raw_config = active_adapter.generate_wireguard_conf(inbound_dict, client_dict, sub_host)
                elif req_proto in ("amneziawg", "awg"):
                    raw_config = active_adapter.generate_awg_conf(inbound_dict, client_dict, sub_host)
                elif req_proto == "openvpn":
                    raw_config = active_adapter.generate_openvpn_conf(inbound_dict, client_dict, sub_host)
    except Exception as e:
        logger.debug(f"Could not generate direct client URL or config: {e}")
    finally:
        if adapter and hasattr(adapter, "client") and hasattr(adapter.client, "close"):
            try:
                await adapter.client.close()
            except Exception:
                pass

    # Robust fallback direct link generation if adapter URL was not produced
    if not direct_link:
        sport = inbound.port if inbound else 443
        sremark = urllib.parse.quote(req.remark or f"{config_prefix}-{target_server.location_code}")
        if req_proto == "vless":
            direct_link = f"vless://{client_uuid}@{sub_host}:{sport}?security=reality&sni={sub_host}&fp=chrome&type=tcp#{sremark}"
        elif req_proto == "vmess":
            import base64
            vm_obj = {
                "v": "2",
                "ps": req.remark or f"{config_prefix}-{target_server.location_code}",
                "add": sub_host,
                "port": sport,
                "id": client_uuid,
                "aid": 0,
                "net": "ws",
                "type": "none",
                "host": sub_host,
                "path": "/",
                "tls": "none"
            }
            direct_link = f"vmess://{base64.b64encode(json.dumps(vm_obj).encode()).decode()}"
        elif req_proto == "trojan":
            direct_link = f"trojan://{client_uuid}@{sub_host}:{sport}?security=tls&sni={sub_host}#{sremark}"
        elif req_proto == "shadowsocks":
            import base64
            ss_auth = base64.b64encode(f"aes-128-gcm:{client_uuid}".encode()).decode()
            direct_link = f"ss://{ss_auth}@{sub_host}:{sport}#{sremark}"
        else:
            direct_link = f"{req_proto}://{client_uuid}@{sub_host}:{sport}#{sremark}"

    # 10. Audit Log & Commit
    await record_audit(
        db,
        "QUICK_CREATE_SUBSCRIPTION",
        f"Created manual subscription for {client_email}",
        admin.id,
        target_type="Subscription",
        target_id=str(sub.id),
    )
    await db.commit()
    await db.refresh(sub)

    # Guarantee subscription_url and direct_link match the dynamic services page exactly
    try:
        sub_svc = SubscriptionService(db)
        conn_details = await sub_svc.get_connection_details(sub.id)
        if conn_details.get("subscription_url"):
            sub_url = conn_details["subscription_url"]
        if conn_details.get("direct_link"):
            direct_link = conn_details["direct_link"]
    except Exception as e_conn:
        logger.warning(f"Could not retrieve dynamic connection details for sub {sub.id}: {e_conn}")

    # 11. Return response
    res_dict = {
        "success": True,
        "subscription_id": str(sub.id),
        "email": client_email,
        "subscription_url": sub_url,
        "uuid": client_uuid,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "direct_link": direct_link,
        "traffic_gb": req.traffic_gb,
        "traffic_limit_bytes": traffic_limit_bytes,
        "server_id": str(target_server.id),
        "server_name": target_server.name,
        "location_name": target_server.location_name,
        "protocol": req_proto.upper(),
    }
    if raw_config is not None:
        res_dict["raw_config"] = raw_config
    return res_dict


@router.post("/subscriptions/sync-traffic")
async def sync_subscriptions_traffic(
    admin: User = Depends(get_current_admin),
):
    """Manually trigger background sync of traffic counters from 3x-ui panels."""
    from app.workers.tasks.sync import _async_sync_all_traffic
    try:
        await _async_sync_all_traffic()
        return {"success": True, "message": "ترافیک مصرفی تمامی کلاینت‌ها با موفقیت از پنل ۳x-ui همگام‌سازی شد."}
    except Exception as e:
        logger.error(f"Error syncing traffic manually: {e}")
        raise HTTPException(status_code=500, detail=f"خطا در همگام‌سازی ترافیک: {str(e)}")


@router.get("/subscriptions/online-clients")
async def get_online_clients(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve online clients from connected 3X-UI panels."""
    panels = (await db.execute(select(Panel).where(Panel.is_active == True))).scalars().all()
    online_set = set()

    for p in panels:
        client = ThreeXUIClient(
            base_url=p.base_url,
            username=p.username,
            password=p.password,
            api_token=p.api_token,
            login_secret=p.login_secret,
            timeout=8.0,
        )
        adapter = ThreeXUIPanelAdapter(client)
        try:
            onlines = await adapter.get_online_clients()
            for email in onlines:
                if email:
                    online_set.add(email)
        except Exception as exc:
            logger.debug(f"Could not fetch onlines from panel {p.name}: {exc}")
        finally:
            await client.close()

    online_list = sorted(list(online_set))
    return {
        "online_count": len(online_list),
        "online_emails": online_list,
    }


@router.get("/subscriptions/{sub_id}/details")
async def get_subscription_details(
    sub_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    sub_svc = SubscriptionService(db)
    try:
        conn = await sub_svc.get_connection_details(sub_id)
        return {"success": True, "data": conn}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/subscriptions/{sub_id}/extend")
async def extend_subscription(
    sub_id: uuid.UUID,
    req: ExtendSubscriptionRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Extend subscription traffic quota and/or duration days."""
    sub = await db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک یافت نشد.")

    if req.add_traffic_gb <= 0 and req.add_days <= 0:
        raise HTTPException(status_code=400, detail="حداقل یکی از مقادیر حجم یا روز را وارد نمایید.")

    # 1. Update traffic limit if specified and not already unlimited
    if req.add_traffic_gb > 0:
        if sub.traffic_limit_bytes > 0:
            sub.traffic_limit_bytes += int(req.add_traffic_gb * (1024**3))

    # 2. Update expiration date if specified
    if req.add_days > 0:
        now = datetime.now(timezone.utc)
        cur_exp = sub.expires_at
        if cur_exp and cur_exp.tzinfo is None:
            cur_exp = cur_exp.replace(tzinfo=timezone.utc)
        base_time = cur_exp if (cur_exp and cur_exp > now) else now
        sub.expires_at = base_time + timedelta(days=req.add_days)

    # 3. Synchronize with 3X-UI remote panel
    if sub.panel_id and sub.inbound_id:
        panel = await db.get(Panel, sub.panel_id)
        inbound = await db.get(Inbound, sub.inbound_id)
        if panel and inbound:
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
                expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
                spec = ClientSpec(
                    id=sub.client_uuid,
                    email=sub.client_email,
                    flow="xtls-rprx-vision" if sub.protocol.lower() == "vless" else "",
                    limitIp=2,
                    totalGB=sub.traffic_limit_bytes,
                    expiryTime=expiry_ms,
                    enable=(sub.status == SubscriptionStatus.ACTIVE),
                    subId=sub.subscription_token,
                )
                await adapter.update_client(inbound.remote_inbound_id, sub.client_uuid, spec)
            except Exception as exc:
                logger.warning(f"Could not update client on 3X-UI panel during extend: {exc}")
            finally:
                await client.close()

    # 4. Record audit log
    await record_audit(
        db,
        action="SUBSCRIPTION_EXTENDED",
        details=f"Extended sub {sub.client_email}: +{req.add_traffic_gb}GB, +{req.add_days} days",
        actor_id=admin.telegram_id,
        target_type="Subscription",
        target_id=str(sub.id),
    )
    await db.commit()

    return {
        "success": True,
        "message": f"اشتراک با موفقیت تمدید شد (+{req.add_traffic_gb} گیگابایت، +{req.add_days} روز).",
        "subscription": {
            "id": str(sub.id),
            "email": sub.client_email,
            "traffic_limit_bytes": sub.traffic_limit_bytes,
            "traffic_used_bytes": sub.traffic_used_bytes,
            "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
            "status": sub.status,
        },
    }


@router.post("/subscriptions/{sub_id}/toggle-status")
async def toggle_subscription_status(
    sub_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle subscription status between ACTIVE and DISABLED."""
    sub = await db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک یافت نشد.")

    new_status = SubscriptionStatus.DISABLED if sub.status == SubscriptionStatus.ACTIVE else SubscriptionStatus.ACTIVE
    sub.status = new_status

    if sub.panel_id and sub.inbound_id:
        panel = await db.get(Panel, sub.panel_id)
        inbound = await db.get(Inbound, sub.inbound_id)
        if panel and inbound:
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
                expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
                spec = ClientSpec(
                    id=sub.client_uuid,
                    email=sub.client_email,
                    flow="xtls-rprx-vision" if sub.protocol.lower() == "vless" else "",
                    limitIp=2,
                    totalGB=sub.traffic_limit_bytes,
                    expiryTime=expiry_ms,
                    enable=(new_status == SubscriptionStatus.ACTIVE),
                    subId=sub.subscription_token,
                )
                await adapter.update_client(inbound.remote_inbound_id, sub.client_uuid, spec)
            except Exception as exc:
                logger.warning(f"Could not update client status on 3X-UI panel: {exc}")
            finally:
                await client.close()

    await record_audit(
        db,
        action="SUBSCRIPTION_STATUS_TOGGLED",
        details=f"Toggled sub {sub.client_email} status to {new_status}",
        actor_id=admin.telegram_id,
        target_type="Subscription",
        target_id=str(sub.id),
    )
    await db.commit()

    return {"success": True, "status": new_status}


@router.post("/subscriptions/{sub_id}/reset-traffic")
async def reset_subscription_traffic(
    sub_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Reset used traffic for a subscription back to 0 bytes."""
    sub = await db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک یافت نشد.")

    sub.traffic_used_bytes = 0

    if sub.panel_id and sub.inbound_id:
        panel = await db.get(Panel, sub.panel_id)
        inbound = await db.get(Inbound, sub.inbound_id)
        if panel and inbound:
            client = ThreeXUIClient(
                base_url=panel.base_url,
                username=panel.username,
                password=panel.password,
                api_token=panel.api_token,
                login_secret=panel.login_secret,
                timeout=10.0,
            )
            try:
                remote_inb = inbound.remote_inbound_id if inbound else 1
                await client.post(f"/panel/api/inbounds/{remote_inb}/resetClientTraffics/{sub.client_email}")
            except Exception as exc:
                logger.warning(f"Could not reset client traffic on 3X-UI: {exc}")
            finally:
                await client.close()

    await record_audit(
        db,
        action="SUBSCRIPTION_TRAFFIC_RESET",
        details=f"Traffic reset to 0 for {sub.client_email}",
        actor_id=admin.telegram_id,
        target_type="Subscription",
        target_id=str(sub.id),
    )
    await db.commit()

    return {
        "success": True,
        "message": f"ترافیک مصرفی اشتراک {sub.client_email} با موفقیت صفر گردید.",
    }


@router.post("/subscriptions/{sub_id}/sync")
async def sync_single_subscription(
    sub_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Sync single subscription details and traffic with remote 3X-UI panel."""
    sub = await db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک یافت نشد.")

    if sub.panel_id and sub.inbound_id:
        panel = await db.get(Panel, sub.panel_id)
        inbound = await db.get(Inbound, sub.inbound_id)
        if panel and inbound:
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
                remote_client = await adapter.get_client_traffic(sub.client_email)
                if remote_client:
                    sub.traffic_used_bytes = int(remote_client.up + remote_client.down)
                    await db.commit()
            except Exception as exc:
                logger.warning(f"Could not sync client traffic with 3X-UI: {exc}")
            finally:
                await client.close()

    return {
        "success": True,
        "message": f"همگام‌سازی اشتراک {sub.client_email} با موفقیت انجام شد.",
        "traffic_used_bytes": sub.traffic_used_bytes,
    }


@router.post("/subscriptions/{sub_id}/change-location")
async def change_subscription_location(
    sub_id: uuid.UUID,
    req: ChangeLocationRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Move subscription to another server location and synchronize 3X-UI."""
    try:
        target_server_id = uuid.UUID(req.server_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="شناسه سرور نامعتبر است.")

    sub = await db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک یافت نشد.")

    target_server = await db.get(Server, target_server_id)
    if not target_server:
        raise HTTPException(status_code=404, detail="سرور مقصد یافت نشد.")

    # Find candidate inbound on new server
    inbound_stmt = (
        select(Inbound)
        .where(
            and_(
                Inbound.panel_id == target_server.panel_id,
                Inbound.is_active == True,
                Inbound.protocol == sub.protocol,
            )
        )
    )
    new_inbound = (await db.execute(inbound_stmt)).scalars().first()
    if not new_inbound:
        fallback_stmt = (
            select(Inbound)
            .where(
                and_(
                    Inbound.panel_id == target_server.panel_id,
                    Inbound.is_active == True,
                )
            )
        )
        new_inbound = (await db.execute(fallback_stmt)).scalars().first()

    if not new_inbound:
        raise HTTPException(status_code=400, detail="هیچ اینباند فعالی برای سرور مقصد یافت نشد.")

    target_panel = await db.get(Panel, target_server.panel_id)
    if not target_panel:
        raise HTTPException(status_code=400, detail="پنل سرور مقصد یافت نشد.")

    # If old panel exists and is different from new, remove client from old panel
    if sub.panel_id and (sub.panel_id != target_panel.id or sub.inbound_id != new_inbound.id):
        old_panel = await db.get(Panel, sub.panel_id)
        old_inbound = await db.get(Inbound, sub.inbound_id) if sub.inbound_id else None
        if old_panel and old_inbound:
            old_client = ThreeXUIClient(
                base_url=old_panel.base_url,
                username=old_panel.username,
                password=old_panel.password,
                api_token=old_panel.api_token,
                login_secret=old_panel.login_secret,
                timeout=10.0,
            )
            old_adapter = ThreeXUIPanelAdapter(old_client)
            try:
                await old_adapter.delete_client(
                    old_inbound.remote_inbound_id,
                    sub.client_uuid,
                    client_email=sub.client_email,
                )
            except Exception as exc:
                logger.warning(f"Could not delete client on old panel during location move: {exc}")
            finally:
                await old_client.close()

    # Add client to target panel/inbound
    new_client = ThreeXUIClient(
        base_url=target_panel.base_url,
        username=target_panel.username,
        password=target_panel.password,
        api_token=target_panel.api_token,
        login_secret=target_panel.login_secret,
        timeout=10.0,
    )
    new_adapter = ThreeXUIPanelAdapter(new_client)
    try:
        expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
        spec = ClientSpec(
            id=sub.client_uuid,
            email=sub.client_email,
            flow="xtls-rprx-vision" if new_inbound.protocol.lower() == "vless" else "",
            limitIp=2,
            totalGB=sub.traffic_limit_bytes,
            expiryTime=expiry_ms,
            enable=(sub.status == SubscriptionStatus.ACTIVE),
            subId=sub.subscription_token,
        )
        await new_adapter.add_client(new_inbound.remote_inbound_id, spec)
    except Exception as exc:
        logger.warning(f"Could not add client on new panel during location move: {exc}")
    finally:
        await new_client.close()

    # Update subscription record
    sub.server_id = target_server.id
    sub.panel_id = target_panel.id
    sub.inbound_id = new_inbound.id
    sub.protocol = new_inbound.protocol.lower()

    await record_audit(
        db,
        action="SUBSCRIPTION_LOCATION_CHANGED",
        details=f"Moved sub {sub.client_email} to server {target_server.name} ({target_server.location_name})",
        actor_id=admin.telegram_id,
        target_type="Subscription",
        target_id=str(sub.id),
    )
    await db.commit()

    return {
        "success": True,
        "server_id": str(target_server.id),
        "location": target_server.location_name,
        "message": f"لوکیشن اشتراک به {target_server.location_name} تغییر یافت.",
    }


@router.post("/subscriptions/{sub_id}/assign-user")
async def assign_subscription_user(
    sub_id: uuid.UUID,
    req: AssignSubscriptionUserRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Assign or change the Telegram user bound to an existing subscription (e.g. manual config)."""
    sub = await db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک مورد نظر یافت نشد.")

    uid_str = (req.user_identifier or "").strip().lstrip("@")
    target_user = None

    if not uid_str or uid_str in ("0", "وب‌پنل", "manual"):
        manual_holder = (await db.execute(select(User).where(User.telegram_id == 0))).scalars().first()
        if not manual_holder:
            manual_holder = User(
                id=uuid.uuid4(),
                telegram_id=0,
                username="manual_clients",
                first_name="مشتری دستی (وب‌پنل)",
                role=UserRole.CUSTOMER,
                is_active=True,
                is_banned=False,
                referral_code="MANUAL_WEB_CLIENTS",
            )
            db.add(manual_holder)
            await db.flush()
        target_user = manual_holder
    else:
        try:
            u_uuid = uuid.UUID(uid_str)
            target_user = await db.get(User, u_uuid)
        except ValueError:
            pass

        if not target_user and uid_str.isdigit():
            tg_num = int(uid_str)
            target_user = (await db.execute(select(User).where(User.telegram_id == tg_num))).scalars().first()
            if not target_user:
                default_name = (req.customer_name or "").strip() or (sub.client_email.split("_")[0] if sub.client_email else f"کاربر {tg_num}")
                target_user = User(
                    id=uuid.uuid4(),
                    telegram_id=tg_num,
                    username=None,
                    first_name=default_name,
                    role=UserRole.CUSTOMER,
                    is_active=True,
                    is_banned=False,
                    referral_code=f"REF_{tg_num}_{secrets.token_hex(2).upper()}",
                )
                db.add(target_user)
                await db.flush()

        if not target_user:
            target_user = (await db.execute(select(User).where(func.lower(User.username) == uid_str.lower()))).scalars().first()

        if not target_user:
            raise HTTPException(
                status_code=404,
                detail="کاربر با این یوزرنیم در دیتابیس ربات یافت نشد. لطفاً آیدی عددی تلگرام مشتری را وارد نمایید (با آیدی عددی، حساب کاربر خودکار ساخته می‌شود).",
            )

    sub.user_id = target_user.id

    # Also update tgId on remote 3X-UI panel if connected
    if sub.panel_id and sub.inbound_id:
        panel = await db.get(Panel, sub.panel_id)
        inbound = await db.get(Inbound, sub.inbound_id)
        if panel and inbound and panel.is_active:
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
                expiry_ms = int(sub.expires_at.timestamp() * 1000) if sub.expires_at else 0
                spec = ClientSpec(
                    id=sub.client_uuid,
                    email=sub.client_email,
                    flow="xtls-rprx-vision" if (sub.protocol or "vless").lower() == "vless" else "",
                    limitIp=2,
                    totalGB=int(sub.traffic_limit_bytes or 0),
                    expiryTime=expiry_ms,
                    enable=(sub.status == SubscriptionStatus.ACTIVE),
                    tgId=int(target_user.telegram_id) if target_user.telegram_id else 0,
                    subId=sub.subscription_token,
                )
                await adapter.update_client(inbound.remote_inbound_id, sub.client_uuid, spec)
            except Exception as exc:
                logger.warning(f"Could not update client tgId on 3X-UI during user assignment: {exc}")
            finally:
                await client.close()

    await record_audit(
        db,
        action="SUBSCRIPTION_USER_ASSIGNED",
        details=f"Assigned sub {sub.client_email} to user {target_user.first_name} (tg_id={target_user.telegram_id})",
        actor_id=admin.telegram_id,
        target_type="Subscription",
        target_id=str(sub.id),
    )
    await db.commit()

    return {
        "success": True,
        "customer_name": target_user.first_name,
        "customer_telegram_id": target_user.telegram_id if target_user.telegram_id else "وب‌پنل",
        "message": (
            f"این سرویس با موفقیت به کاربر تلگرام «{target_user.first_name}» ({target_user.telegram_id}) متصل شد و اکنون در بخش «سرویس‌های من» ربات برای ایشان نمایش داده می‌شود."
            if target_user.telegram_id
            else "این سرویس از اکانت تلگرام جدا شد و به مشتری دستی (وب‌پنل) منتقل گردید."
        ),
    }



@router.delete("/subscriptions/{sub_id}")
async def delete_subscription(
    sub_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    sub = await db.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="سرویس یافت نشد.")

    client_email_copy = sub.client_email
    sub_id_copy = str(sub.id)

    # Remove from 3x-ui panel across all inbounds
    if sub.panel_id:
        panel = await db.get(Panel, sub.panel_id)
        inbound = await db.get(Inbound, sub.inbound_id) if sub.inbound_id else None
        if panel:
            from app.integrations.xui.client import ThreeXUIClient
            from app.integrations.xui.adapter import ThreeXUIPanelAdapter
            panel_inbs = (await db.execute(select(Inbound).where(Inbound.panel_id == panel.id))).scalars().all()
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
                remote_inb = inbound.remote_inbound_id if inbound else 1
                await adapter.delete_client(
                    remote_inb,
                    sub.client_uuid,
                    client_email=sub.client_email,
                    all_inbound_ids=all_remote_ids,
                )
            except Exception as e:
                logger.warning(f"Could not delete on panel: {e}")
            finally:
                await client.close()

    # Decrement capacity counters and permanently delete subscription from DB
    if sub.server_id:
        srv = await db.get(Server, sub.server_id)
        if srv and srv.current_clients and srv.current_clients > 0:
            srv.current_clients -= 1
    if sub.inbound_id:
        inb = await db.get(Inbound, sub.inbound_id)
        if inb and inb.client_count and inb.client_count > 0:
            inb.client_count -= 1

    await db.delete(sub)
    await record_audit(
        db,
        action="SUBSCRIPTION_DELETED",
        details=f"Deleted subscription {client_email_copy}",
        actor_id=admin.telegram_id,
        target_type="Subscription",
        target_id=sub_id_copy,
    )
    await db.commit()
    return {"success": True, "message": "سرویس با موفقیت از پنل و دیتابیس حذف گردید."}


# -------------------------------------------------------------
# 6. Support Tickets Management
# -------------------------------------------------------------
@router.get("/tickets")
async def list_tickets(
    status_filter: Optional[str] = Query(None, description="open, closed, all"),
    limit: int = 500,
    offset: int = 0,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(SupportTicket).options(selectinload(SupportTicket.user), selectinload(SupportTicket.messages))

    if status_filter == "open":
        query = query.where(SupportTicket.status.in_([TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_FOR_CUSTOMER]))
    elif status_filter == "closed":
        query = query.where(SupportTicket.status.in_([TicketStatus.CLOSED, TicketStatus.RESOLVED]))

    total = (await db.execute(select(func.count(SupportTicket.id)))).scalar() or 0
    query = query.order_by(SupportTicket.updated_at.desc()).offset(offset).limit(limit)
    tickets = (await db.execute(query)).scalars().all()

    items = []
    for t in tickets:
        items.append({
            "id": str(t.id),
            "short_id": t.id.hex[:6],
            "customer_name": t.user.first_name if t.user else "ناشناس",
            "customer_telegram_id": t.user.telegram_id if t.user else None,
            "subject": t.subject,
            "category": t.category,
            "status": t.status,
            "messages_count": len(t.messages),
            "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "—",
        })

    return {"total": total, "items": items}


@router.get("/tickets/{ticket_id}")
async def get_ticket_conversation(
    ticket_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.user), selectinload(SupportTicket.messages))
        .where(SupportTicket.id == ticket_id)
    )
    ticket = res.scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=404, detail="تیکت یافت نشد.")

    messages = [
        {
            "id": str(m.id),
            "sender_type": m.sender_type,
            "text": m.text,
            "attachment_file_id": m.attachment_file_id,
            "created_at": m.created_at.strftime("%H:%M - %Y/%m/%d"),
        }
        for m in ticket.messages
    ]

    return {
        "id": str(ticket.id),
        "short_id": ticket.id.hex[:6],
        "subject": ticket.subject,
        "status": ticket.status,
        "customer_name": ticket.user.first_name if ticket.user else "کاربر",
        "customer_telegram_id": ticket.user.telegram_id if ticket.user else None,
        "messages": messages,
    }


@router.post("/tickets/{ticket_id}/reply")
async def reply_to_ticket(
    ticket_id: uuid.UUID,
    req: TicketReplyRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    support_svc = SupportService(db)
    ticket = await db.get(SupportTicket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="تیکت یافت نشد.")

    # Record message in DB
    await support_svc.add_reply(
        ticket_id=ticket.id,
        sender_type="ADMIN",
        sender_id=admin.telegram_id,
        text=req.message,
    )

    # Deliver message to user in Telegram
    bot = get_telegram_bot()
    delivered = False
    delivery_err = ""
    try:
        user = await db.get(User, ticket.user_id)
        if user and user.telegram_id:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"🎧 <b>پاسخ پشتیبانی به تیکت #{ticket.id.hex[:6]}:</b>\n\n"
                    f"{req.message}\n\n"
                    f"💬 <i>در صورت نیاز می‌توانید به همین پیام پاسخ دهید.</i>"
                ),
                parse_mode="HTML",
            )
            delivered = True
    except Exception as e_tg:
        delivery_err = str(e_tg)
        logger.warning(f"Could not deliver ticket reply to telegram user: {e_tg}")
    finally:
        await bot.session.close()

    if not delivered:
        await db.commit()
        return {
            "success": False,
            "message": f"پیام در دیتابیس ثبت شد اما ارسال به تلگرام کاربر با خطا مواجه گردید ({delivery_err}). تیکت باز نگه داشته شد.",
        }

    if req.close_after_reply:
        ticket.status = TicketStatus.CLOSED
    else:
        ticket.status = TicketStatus.WAITING_FOR_CUSTOMER

    await db.commit()
    return {"success": True, "message": "پاسخ با موفقیت برای کاربر در تلگرام ارسال شد."}


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    ticket = await db.get(SupportTicket, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="تیکت یافت نشد.")

    ticket.status = TicketStatus.CLOSED
    await db.commit()
    return {"success": True, "message": "تیکت با موفقیت بسته شد."}


# -------------------------------------------------------------
# 7. 3X-UI Panels, Servers, and Inbounds Management
# -------------------------------------------------------------
@router.get("/panels")
async def list_panels(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all connected 3X-UI panels with inbounds, servers, and live status."""
    panels_res = await db.execute(
        select(Panel).options(selectinload(Panel.inbounds), selectinload(Panel.servers)).order_by(Panel.created_at.desc())
    )
    panels = panels_res.scalars().all()

    items = []
    for p in panels:
        subs_cnt = (
            await db.execute(
                select(func.count(Subscription.id)).where(Subscription.panel_id == p.id)
            )
        ).scalar() or 0

        active_subs_cnt = (
            await db.execute(
                select(func.count(Subscription.id)).where(
                    Subscription.panel_id == p.id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                )
            )
        ).scalar() or 0

        items.append({
            "id": str(p.id),
            "name": p.name,
            "base_url": p.base_url,
            "panel_type": getattr(p, "panel_type", "3x-ui") or "3x-ui",
            "username": p.username,
            "version": p.version,
            "is_active": p.is_active,
            "is_healthy": p.is_healthy,
            "error_message": p.error_message,
            "last_sync_at": p.last_sync_at.isoformat() if p.last_sync_at else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "inbounds_count": len(p.inbounds) if p.inbounds else 0,
            "servers_count": len(p.servers) if p.servers else 0,
            "total_subscriptions": subs_cnt,
            "active_subscriptions": active_subs_cnt,
        })
    return {"panels": items}


@router.post("/panels")
async def create_panel(
    req: PanelCreateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add a new 3X-UI or VPN-UI panel, authenticate, sync remote inbounds, and register Server entity."""
    from app.integrations.xui.client import normalize_panel_base_url
    raw_url = req.base_url.strip()
    name = req.name.strip()
    username = req.username.strip()
    password = req.password.strip()
    panel_type = (req.panel_type or "3x-ui").strip().lower()
    api_token = req.api_token.strip() if req.api_token else None
    login_secret = (req.login_secret or req.otp_secret or "").strip() or None

    if not name or not raw_url or not username or not password:
        raise HTTPException(status_code=400, detail="تمامی فیلدهای الزامی پنل را تکمیل فرمایید.")

    if len(raw_url) > 2048 or len(raw_url) < 8:
        raise HTTPException(status_code=400, detail="آدرس پنل نامعتبر است یا طول آن بیش از حد مجاز است.")

    base_url = normalize_panel_base_url(raw_url)

    if len(name) > 128:
        raise HTTPException(status_code=400, detail="نام پنل نمی‌تواند بیش از ۱۲۸ کاراکتر باشد.")

    if len(username) > 128:
        raise HTTPException(status_code=400, detail="نام کاربری پنل نمی‌تواند بیش از ۱۲۸ کاراکتر باشد.")

    if panel_type not in ("3x-ui", "vpn-ui"):
        raise HTTPException(status_code=400, detail=f"نوع پنل نامعتبر است ({panel_type}). مقادیر مجاز: 3x-ui یا vpn-ui")

    # Check for name/url duplication
    existing = (
        await db.execute(
            select(Panel).where(or_(Panel.name == name, Panel.base_url == base_url))
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="پنلی با این نام یا آدرس قبلاً در سیستم ثبت شده است.")

    # 1. Live test connection and login
    temp_panel = Panel(
        name=name,
        base_url=base_url,
        panel_type=panel_type,
        username=username,
    )
    temp_panel.password = password
    if api_token:
        temp_panel.api_token = api_token
    if login_secret:
        temp_panel.login_secret = login_secret

    client = get_panel_client(temp_panel, timeout=12.0)
    try:
        is_ok = await client.login()
        if not is_ok:
            raise HTTPException(status_code=400, detail="احراز هویت با نام کاربری یا کلمه عبور وارد شده ناموفق بود. لطفاً دسترسی‌ها را بررسی نمایید.")
        
        # 2. Fetch inbounds
        inbounds_data = await client.get_inbounds()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"خطا در اتصال به پنل: {str(exc)}")
    finally:
        await client.close()

    # 3. Create Panel record
    panel = Panel(
        id=uuid.uuid4(),
        name=name,
        base_url=base_url,
        panel_type=panel_type,
        username=username,
        version=req.version or "v3.11",
        is_active=True,
        is_healthy=True,
        last_sync_at=datetime.now(timezone.utc),
    )
    panel.password = password
    if api_token:
        panel.api_token = api_token
    if login_secret:
        panel.login_secret = login_secret
    db.add(panel)
    await db.flush()

    # 4. Sync inbounds into DB
    saved_inbounds = 0
    created_inbounds = []
    for inb in inbounds_data:
        rem_id = inb.get("id")
        if rem_id is None:
            continue
        stream_raw = inb.get("streamSettings")
        stream_str = json.dumps(stream_raw) if isinstance(stream_raw, (dict, list)) else (str(stream_raw) if stream_raw else "")
        proto_str = str(inb.get("protocol", "vless")).lower()
        port_val = int(inb.get("port", 443))
        listen_val = str(inb.get("listen", "0.0.0.0"))
        tag_val = str(inb.get("tag", ""))
        remark_val = str(inb.get("remark", ""))
        enable_val = bool(inb.get("enable", True))

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
        db.add(db_inb)
        created_inbounds.append(db_inb)
        saved_inbounds += 1

    await db.flush()

    # 5. Automatically convert synced inbounds to Server records
    from app.modules.servers.allocation import sync_inbounds_to_servers
    if created_inbounds:
        await sync_inbounds_to_servers(db, panel, created_inbounds)
    else:
        # Fallback if panel has 0 inbounds
        import urllib.parse
        parsed = urllib.parse.urlparse(base_url)
        sub_host = parsed.hostname or base_url
        srv = Server(
            id=uuid.uuid4(),
            name=f"سرور {name}",
            location_code="ALL",
            location_name=name,
            flag_emoji="🌐",
            priority=10,
            panel_id=panel.id,
            subscription_host=sub_host,
            subscription_port=parsed.port or 443,
            is_active=True,
            capacity_limit=1000,
        )
        db.add(srv)

    panel_title = "VPN-UI" if panel_type == "vpn-ui" else "3X-UI"
    await record_audit(
        db,
        action="PANEL_ADDED",
        details=f"Added {panel_title} panel {name} ({base_url}) with {saved_inbounds} inbounds",
        actor_id=admin.telegram_id,
        target_type="Panel",
        target_id=str(panel.id),
    )
    await db.commit()

    return {
        "success": True,
        "message": f"پنل {panel_title} «{name}» با موفقیت افزوده و متصل شد ({saved_inbounds} اینباند شناسایی گردید).",
        "panel_id": str(panel.id),
        "panel_type": panel_type,
        "inbounds_count": saved_inbounds,
    }


@router.post("/panels/{panel_id}/sync")
async def sync_panel_inbounds_endpoint(
    panel_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Re-synchronize inbounds from the remote panel into local database."""
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="پنل یافت نشد.")

    adapter = get_panel_adapter(panel, timeout=12.0)
    try:
        ok = await adapter.client.login()
        if not ok:
            panel.is_healthy = False
            panel.error_message = "احراز هویت با پنل ناموفق بود."
            try:
                from app.services.panel_health_service import PanelHealthService
                await PanelHealthService.record_panel_result(
                    panel=panel,
                    is_healthy=False,
                    error_msg="احراز هویت با پنل ناموفق بود.",
                    db_session=db,
                )
            except Exception:
                pass
            await db.commit()
            raise HTTPException(status_code=400, detail="ورود به پنل ناموفق بود. لطفاً رمز عبور را بررسی فرمایید.")

        inbounds_data = await adapter.list_inbounds()
        panel.is_healthy = True
        panel.error_message = None
        panel.last_sync_at = datetime.now(timezone.utc)
        try:
            from app.services.panel_health_service import PanelHealthService
            await PanelHealthService.record_panel_result(
                panel=panel,
                is_healthy=True,
                error_msg="",
                db_session=db,
            )
        except Exception:
            pass
    except HTTPException:
        raise
    except Exception as exc:
        panel.is_healthy = False
        panel.error_message = str(exc)
        try:
            from app.services.panel_health_service import PanelHealthService
            await PanelHealthService.record_panel_result(
                panel=panel,
                is_healthy=False,
                error_msg=f"خطا در ارتباط با پنل: {exc}",
                db_session=db,
            )
        except Exception:
            pass
        await db.commit()
        raise HTTPException(status_code=400, detail=f"خطا در ارتباط با پنل: {exc}")
    finally:
        await adapter.client.close()

    synced_count = 0
    for inb in inbounds_data:
        if hasattr(inb, "id"):
            rem_id = inb.id
            stream_raw = inb.stream_settings
            proto_str = str(inb.protocol or "vless").lower()
            port_val = int(inb.port or 443)
            listen_val = str(inb.listen or "0.0.0.0")
            tag_val = str(inb.tag or "")
            remark_val = str(inb.remark or "")
            enable_val = bool(inb.enable if inb.enable is not None else True)
        else:
            rem_id = inb.get("id")
            stream_raw = inb.get("streamSettings")
            proto_str = str(inb.get("protocol", "vless")).lower()
            port_val = int(inb.get("port", 443))
            listen_val = str(inb.get("listen", "0.0.0.0"))
            tag_val = str(inb.get("tag", ""))
            remark_val = str(inb.get("remark", ""))
            enable_val = bool(inb.get("enable", True))

        if rem_id is None:
            continue

        inb_res = await db.execute(
            select(Inbound).where(
                and_(Inbound.panel_id == panel.id, Inbound.remote_inbound_id == rem_id)
            )
        )
        db_inb = inb_res.scalar_one_or_none()

        stream_str = json.dumps(stream_raw) if isinstance(stream_raw, (dict, list)) else (str(stream_raw) if stream_raw else "")

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
            db.add(db_inb)
        else:
            db_inb.protocol = proto_str
            db_inb.port = port_val
            db_inb.listen = listen_val
            db_inb.tag = tag_val
            db_inb.remark = remark_val
            db_inb.stream_settings = stream_str
            if not enable_val:
                db_inb.is_active = False
            # If enable_val is True, preserve existing db_inb.is_active state from database
        synced_count += 1

    await db.flush()
    # Synchronize inbounds into Server entities immediately
    from app.modules.servers.allocation import sync_inbounds_to_servers
    all_panel_inbounds = (await db.execute(
        select(Inbound).where(Inbound.panel_id == panel.id)
    )).scalars().all()
    await sync_inbounds_to_servers(db, panel, all_panel_inbounds)

    await record_audit(
        db,
        action="PANEL_SYNCED",
        details=f"Synced {synced_count} inbounds for panel {panel.name} (preserved active states)",
        actor_id=admin.telegram_id,
        target_type="Panel",
        target_id=str(panel.id),
    )
    await db.commit()

    return {
        "success": True,
        "message": f"همگام‌سازی پنل «{panel.name}» با موفقیت انجام شد ({synced_count} اینباند به‌روزرسانی شد و وضعیت فعال/خاموش دیتابیس حفظ گردید).",
        "synced_count": synced_count,
    }


@router.post("/panels/sync-all")
async def sync_all_panels_inbounds(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Sync inbounds from all active panels into database, preserving active/disabled states."""
    panels = (await db.execute(select(Panel).where(Panel.is_active == True))).scalars().all()
    total_synced = 0
    results = []
    for p in panels:
        try:
            res = await sync_panel_inbounds_endpoint(panel_id=p.id, admin=admin, db=db)
            total_synced += res.get("synced_count", 0)
            results.append({"panel_name": p.name, "success": True, "count": res.get("synced_count", 0)})
        except Exception as e:
            results.append({"panel_name": p.name, "success": False, "error": str(e)})
    return {
        "success": True,
        "message": f"همگام‌سازی تمامی پنل‌ها با موفقیت انجام شد ({total_synced} اینباند به‌روزرسانی شد و وضعیت فعال/خاموش دیتابیس حفظ گردید).",
        "total_synced": total_synced,
        "panels": results,
    }


@router.post("/panels/{panel_id}/test")
async def test_panel_connection_endpoint(
    panel_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Test ping latency, capabilities, and live connection to a specific panel."""
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="پنل یافت نشد.")

    t0 = time.perf_counter()
    client = get_panel_client(panel, timeout=10.0)
    p_type = getattr(panel, "panel_type", "3x-ui") or "3x-ui"
    try:
        ok = await client.login()
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        if ok:
            inbs = await client.get_inbounds()
            capabilities = {}
            if hasattr(client, "get_capabilities"):
                try:
                    capabilities = await client.get_capabilities()
                except Exception:
                    capabilities = {
                        "panel_type": p_type,
                        "is_vpn_ui": True,
                        "protocols": ["wireguard", "amneziawg", "openvpn"],
                    }
            else:
                protocols = sorted(list({ (inb.get("protocol") or "").lower() for inb in inbs if inb.get("protocol") }))
                capabilities = {
                    "panel_type": p_type,
                    "is_vpn_ui": False,
                    "has_wireguard": "wireguard" in protocols,
                    "has_amneziawg": any(p in protocols for p in ("amneziawg", "awg")),
                    "has_openvpn": "openvpn" in protocols,
                    "active_protocols": protocols,
                    "inbounds_count": len(inbs),
                }

            # Check live Xray core status
            xray_state = "running"
            xray_error = ""
            xray_version = ""
            try:
                raw_status = await client.get_status()
                s_obj = raw_status.get("obj") if isinstance(raw_status, dict) else {}
                if isinstance(s_obj, dict):
                    xray_obj = s_obj.get("xray") if isinstance(s_obj.get("xray"), dict) else {}
                    xray_state = str(xray_obj.get("state") or "running").strip().lower()
                    xray_error = str(xray_obj.get("errorMsg") or "").strip()
                    xray_version = str(xray_obj.get("version") or "").strip()
            except Exception as st_err:
                logger.debug(f"Could not read xray status from {panel.name}: {st_err}")

            from app.services.panel_health_service import PanelHealthService
            if xray_error or (xray_state and xray_state != "running"):
                err_desc = xray_error or f"هسته Xray متوقف است (وضعیت: {xray_state})"
                panel.is_healthy = False
                panel.error_message = err_desc
                await db.commit()
                try:
                    await PanelHealthService.record_panel_result(
                        panel=panel,
                        is_healthy=False,
                        error_msg=err_desc,
                        db_session=db,
                        details={"xray_state": xray_state, "xray_error": xray_error, "xray_version": xray_version},
                    )
                except Exception:
                    pass
                return {
                    "success": False,
                    "latency_ms": duration_ms,
                    "inbounds_count": len(inbs),
                    "panel_type": p_type,
                    "xray_state": xray_state,
                    "xray_error": xray_error,
                    "capabilities": capabilities,
                    "message": f"اتصال به پنل برقرار شد، اما هسته Xray خطا دارد: {err_desc}",
                }

            panel.is_healthy = True
            panel.error_message = None
            panel.last_sync_at = datetime.now(timezone.utc)
            await db.commit()
            try:
                await PanelHealthService.record_panel_result(
                    panel=panel,
                    is_healthy=True,
                    error_msg="",
                    db_session=db,
                    details={"xray_state": xray_state, "xray_version": xray_version},
                )
            except Exception:
                pass
            return {
                "success": True,
                "latency_ms": duration_ms,
                "inbounds_count": len(inbs),
                "panel_type": p_type,
                "xray_state": xray_state,
                "capabilities": capabilities,
                "message": f"اتصال به پنل و هسته Xray با موفقیت برقرار شد (پینگ: {duration_ms}ms — اینباندها: {len(inbs)})",
            }
        else:
            panel.is_healthy = False
            panel.error_message = "نام کاربری یا کلمه عبور نامعتبر است."
            await db.commit()
            try:
                from app.services.panel_health_service import PanelHealthService
                await PanelHealthService.record_panel_result(
                    panel=panel,
                    is_healthy=False,
                    error_msg="نام کاربری یا کلمه عبور نامعتبر است.",
                    db_session=db,
                )
            except Exception:
                pass
            return {
                "success": False,
                "latency_ms": duration_ms,
                "panel_type": p_type,
                "message": "عدم امکان احراز هویت (یوزرنیم یا پسورد اشتباه است)",
            }
    except Exception as exc:
        duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        panel.is_healthy = False
        panel.error_message = str(exc)
        await db.commit()
        try:
            from app.services.panel_health_service import PanelHealthService
            await PanelHealthService.record_panel_result(
                panel=panel,
                is_healthy=False,
                error_msg=f"خطا در برقراری ارتباط: {str(exc)}",
                db_session=db,
            )
        except Exception:
            pass
        return {
            "success": False,
            "latency_ms": duration_ms,
            "panel_type": p_type,
            "message": f"خطا در برقراری ارتباط: {str(exc)}",
        }
    finally:
        await client.close()


@router.post("/panels/{panel_id}/toggle")
async def toggle_panel_status_endpoint(
    panel_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle panel active/inactive state."""
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="پنل یافت نشد.")

    panel.is_active = not panel.is_active
    await record_audit(
        db,
        action="PANEL_STATUS_TOGGLED",
        details=f"Toggled panel {panel.name} active={panel.is_active}",
        actor_id=admin.telegram_id,
        target_type="Panel",
        target_id=str(panel.id),
    )
    await db.commit()
    status_str = "فعال" if panel.is_active else "غیرفعال"
    return {"success": True, "is_active": panel.is_active, "message": f"پنل «{panel.name}» با موفقیت {status_str} شد."}


@router.post("/panels/{panel_id}/edit")
async def edit_panel_endpoint(
    panel_id: uuid.UUID,
    req: PanelUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Edit an existing 3X-UI panel's name, URL, or credentials."""
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="پنل یافت نشد.")

    from app.integrations.xui.client import normalize_panel_base_url
    if req.panel_type is not None:
        cleaned_pt = req.panel_type.strip().lower()
        if cleaned_pt not in ("3x-ui", "vpn-ui"):
            raise HTTPException(
                status_code=400,
                detail=f"نوع پنل نامعتبر است ({cleaned_pt}). مقادیر مجاز: 3x-ui یا vpn-ui",
            )
        panel.panel_type = cleaned_pt

    if req.name is not None:
        if not req.name.strip():
            raise HTTPException(status_code=400, detail="نام پنل نمی‌تواند خالی باشد.")
        if len(req.name.strip()) > 128:
            raise HTTPException(status_code=400, detail="نام پنل بیش از ۱۲۸ کاراکتر است.")
        panel.name = req.name.strip()

    if req.base_url is not None:
        if not req.base_url.strip():
            raise HTTPException(status_code=400, detail="آدرس پنل نمی‌تواند خالی باشد.")
        if len(req.base_url.strip()) > 2048:
            raise HTTPException(status_code=400, detail="آدرس پنل بیش از حد مجاز است.")
        panel.base_url = normalize_panel_base_url(req.base_url.strip())

    if req.username is not None:
        if not req.username.strip():
            raise HTTPException(status_code=400, detail="نام کاربری نمی‌تواند خالی باشد.")
        if len(req.username.strip()) > 128:
            raise HTTPException(status_code=400, detail="نام کاربری پنل بیش از ۱۲۸ کاراکتر است.")
        panel.username = req.username.strip()

    if req.password is not None and req.password.strip():
        panel.password = req.password.strip()
    if req.api_token is not None:
        panel.api_token = req.api_token.strip() or None
    if req.login_secret is not None:
        panel.login_secret = req.login_secret.strip() or None
    elif req.otp_secret is not None:
        panel.login_secret = req.otp_secret.strip() or None
    if req.version and req.version.strip():
        panel.version = req.version.strip()

    await record_audit(
        db,
        action="PANEL_UPDATED",
        details=f"Updated panel {panel.name} ({panel.id})",
        actor_id=admin.telegram_id,
        target_type="Panel",
        target_id=str(panel.id),
    )
    await db.commit()
    return {"success": True, "message": f"مشخصات پنل «{panel.name}» با موفقیت به‌روزرسانی شد."}


@router.delete("/panels/{panel_id}")
async def delete_panel_endpoint(
    panel_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a 3X-UI panel and its associated servers/inbounds."""
    panel = await db.get(Panel, panel_id)
    if not panel:
        raise HTTPException(status_code=404, detail="پنل یافت نشد.")

    active_subs = (
        await db.execute(
            select(func.count(Subscription.id)).where(
                Subscription.panel_id == panel.id,
                Subscription.status == SubscriptionStatus.ACTIVE,
            )
        )
    ).scalar() or 0

    if active_subs > 0:
        raise HTTPException(
            status_code=400,
            detail=f"امکان حذف این پنل وجود ندارد زیرا تعداد {active_subs} اشتراک فعال روی آن تعریف شده است. لطفاً ابتدا اشتراک‌ها را به سرور دیگری انتقال دهید یا منقضی نمایید.",
        )

    # For non-active (expired/disabled) subscriptions, remove them safely to preserve DB foreign key integrity
    non_active_subs = (
        await db.execute(
            select(Subscription).where(Subscription.panel_id == panel.id)
        )
    ).scalars().all()
    for nas in non_active_subs:
        await db.delete(nas)

    servers = (await db.execute(select(Server).where(Server.panel_id == panel.id))).scalars().all()
    for s in servers:
        await db.delete(s)

    inbounds = (await db.execute(select(Inbound).where(Inbound.panel_id == panel.id))).scalars().all()
    for ib in inbounds:
        await db.delete(ib)

    panel_name = panel.name
    await db.delete(panel)
    await record_audit(
        db,
        action="PANEL_DELETED",
        details=f"Deleted panel {panel_name} ({panel_id})",
        actor_id=admin.telegram_id,
        target_type="Panel",
        target_id=str(panel_id),
    )
    await db.commit()
    return {"success": True, "message": f"پنل «{panel_name}» و سرورهای متناظر آن با موفقیت حذف شدند."}


@router.post("/panels/test-all")
async def test_all_panels_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ping and check connection for all configured panels."""
    panels = (await db.execute(select(Panel))).scalars().all()
    from app.integrations.factory import get_panel_client
    results = []

    for p in panels:
        t0 = time.perf_counter()
        client = get_panel_client(p, timeout=8.0)
        try:
            ok = await client.login()
            ms = round((time.perf_counter() - t0) * 1000, 1)
            p.is_healthy = ok
            p.error_message = None if ok else "احراز هویت ناموفق بود."
            p.last_sync_at = datetime.now(timezone.utc)
            results.append({
                "id": str(p.id),
                "name": p.name,
                "success": ok,
                "latency_ms": ms,
            })
        except Exception as exc:
            ms = round((time.perf_counter() - t0) * 1000, 1)
            p.is_healthy = False
            p.error_message = str(exc)
            results.append({
                "id": str(p.id),
                "name": p.name,
                "success": False,
                "latency_ms": ms,
                "error": str(exc),
            })
        finally:
            await client.close()

    await db.commit()
    return {"results": results}


@router.get("/servers")
async def list_servers(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    servers = (await db.execute(select(Server).order_by(Server.priority.desc(), Server.name))).scalars().all()
    inbounds = (await db.execute(select(Inbound).order_by(Inbound.port.asc()))).scalars().all()
    panels_res = await db.execute(select(Panel))
    panel_map = {p.id: p.name for p in panels_res.scalars().all()}

    import re
    from app.modules.servers.allocation import LOCATION_TO_INBOUND_ID, COUNTRY_NAME_MAP

    def _find_best_inbound(loc_code: str):
        loc_u = (loc_code or "").upper()
        country = loc_u.split("-")[0]
        num = loc_u.split("-")[1] if "-" in loc_u else ""
        num_str = f"0{num}" if len(num) == 1 else num

        # Priority 0: Direct match by Inbound ID mapping
        target_ids = LOCATION_TO_INBOUND_ID.get(loc_u) or LOCATION_TO_INBOUND_ID.get(country, [])
        for tid in target_ids:
            for inb in inbounds:
                if inb.remote_inbound_id == tid:
                    return inb

        # Priority 1: Match by country name or city alias
        aliases = COUNTRY_NAME_MAP.get(country, [])
        if aliases:
            for inb in inbounds:
                r_u = (inb.remark or inb.tag or "").upper()
                if any(a in r_u for a in aliases):
                    return inb

        # Priority 2: Exact country + sub-number (e.g. FR-1 -> DEC-FR-01)
        if num:
            for inb in inbounds:
                r_u = (inb.remark or inb.tag or "").upper()
                if (
                    f"-{country}-{num}" in r_u
                    or f"-{country}-{num_str}" in r_u
                    or (f"-{country}-" in r_u and (f"-{num}" in r_u or f"-{num_str}" in r_u))
                ):
                    return inb

        # Priority 3: Hyphen-delimited country code (e.g. DEC-DE, DEC-AZ, DEC-AR, DEC-NO, DEC-PL)
        for inb in inbounds:
            r_u = (inb.remark or inb.tag or "").upper()
            if re.search(rf"(?:^|[\s\-_]){re.escape(country)}(?:[\s\-_]|$|\d+)", r_u):
                return inb

        # Priority 4: Special mappings
        if country == "FI":
            for inb in inbounds:
                if "WARP" in (inb.remark or inb.tag or "").upper():
                    return inb
        if country == "DE":
            for inb in inbounds:
                if (inb.remark or inb.tag or "").upper() in ("DEC-DE", "DEC"):
                    return inb
        if country == "SE":
            for inb in inbounds:
                if "WARP" in (inb.remark or inb.tag or "").upper() or (inb.remark or inb.tag or "").upper() == "DEC":
                    return inb
        if country == "AE":
            for inb in inbounds:
                if (inb.remark or inb.tag or "").upper() == "DEC":
                    return inb
        if country == "CY":
            for inb in inbounds:
                if "AZ" in (inb.remark or inb.tag or "").upper():
                    return inb
        if country == "US":
            for inb in inbounds:
                if "US" in (inb.remark or inb.tag or "").upper():
                    return inb
        return None

    inbound_to_servers = {}
    items = []
    for s in servers:
        matched_inb = None
        if getattr(s, "inbound_id", None):
            for inb in inbounds:
                if inb.id == s.inbound_id:
                    matched_inb = inb
                    break
        if not matched_inb:
            matched_inb = _find_best_inbound(s.location_code)
        inb_info = ""
        inb_active = True
        if matched_inb:
            inb_info = f"اینباند {matched_inb.remote_inbound_id}: {matched_inb.remark or matched_inb.tag} (پورت {matched_inb.port})"
            srv_label = f"{s.flag_emoji or ''} {s.name} ({s.location_code})".strip()
            inbound_to_servers.setdefault(str(matched_inb.id), []).append(srv_label)
            inb_active = bool(matched_inb.is_active)

        needs_disable = bool(s.is_active and matched_inb and not inb_active)

        items.append({
            "id": str(s.id),
            "name": s.name,
            "panel_id": str(s.panel_id) if s.panel_id else None,
            "panel_name": panel_map.get(s.panel_id, "نامشخص"),
            "location_code": s.location_code,
            "location_name": s.location_name,
            "flag": s.flag_emoji,
            "host": s.subscription_host,
            "port": s.subscription_port,
            "is_active": s.is_active,
            "clients": s.current_clients,
            "capacity": s.capacity_limit,
            "inbound_info": inb_info,
            "inbound_id": matched_inb.remote_inbound_id if matched_inb else None,
            "inbound_tag": (matched_inb.remark or matched_inb.tag) if matched_inb else None,
            "inbound_is_active": inb_active,
            "needs_disable": needs_disable,
        })

    inb_items = [
        {
            "id": str(ib.id),
            "panel_id": str(ib.panel_id) if ib.panel_id else None,
            "panel_name": panel_map.get(ib.panel_id, "نامشخص"),
            "remote_id": ib.remote_inbound_id,
            "tag": ib.tag,
            "remark": ib.remark,
            "port": ib.port,
            "protocol": ib.protocol,
            "is_active": ib.is_active,
            "connected_servers": inbound_to_servers.get(str(ib.id), []),
        }
        for ib in inbounds
    ]

    return {"servers": items, "inbounds": inb_items}


@router.post("/servers/disable-inactive-inbound-servers")
async def disable_inactive_inbound_servers(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Batch toggle off servers whose connected inbounds are disabled/inactive."""
    servers = (await db.execute(select(Server).where(Server.is_active.is_(True)))).scalars().all()
    inbounds = (await db.execute(select(Inbound))).scalars().all()

    from app.modules.servers.allocation import LOCATION_TO_INBOUND_ID
    inb_map = {ib.remote_inbound_id: ib for ib in inbounds}

    disabled_count = 0
    disabled_names = []
    inb_by_id = {ib.id: ib for ib in inbounds}
    for s in servers:
        matched = None
        if getattr(s, "inbound_id", None) and s.inbound_id in inb_by_id:
            matched = inb_by_id[s.inbound_id]
        if not matched:
            target_ids = LOCATION_TO_INBOUND_ID.get(s.location_code.upper(), [])
            for tid in target_ids:
                if tid in inb_map:
                    matched = inb_map[tid]
                    break
        if matched and not matched.is_active:
            s.is_active = False
            disabled_count += 1
            disabled_names.append(s.name)

    await db.commit()
    await record_audit(
        db,
        action="SERVERS_BATCH_DISABLED",
        details=f"Disabled {disabled_count} servers with inactive inbounds: {', '.join(disabled_names)}",
        actor_id=admin.telegram_id,
        target_type="Server",
    )
    return {
        "success": True,
        "disabled_count": disabled_count,
        "disabled_servers": disabled_names,
        "message": f"{disabled_count} سرور با موفقیت خاموش گردید.",
    }


async def _measure_tcp_ping(host: str, port: int = 443, timeout: float = 1.5) -> Optional[float]:
    if not host or not str(host).strip():
        return None
    cleaned_host = str(host).strip()
    if "://" in cleaned_host:
        from urllib.parse import urlparse
        parsed = urlparse(cleaned_host)
        cleaned_host = parsed.hostname or cleaned_host

    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cleaned_host, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return round((time.perf_counter() - t0) * 1000, 1)
    except Exception:
        if port not in (80, 443):
            try:
                t0 = time.perf_counter()
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(cleaned_host, 443),
                    timeout=timeout,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return round((time.perf_counter() - t0) * 1000, 1)
            except Exception:
                pass
        return None


@router.get("/servers/health")
async def check_servers_health(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Check realtime latency and health of active servers and registered panels."""
    servers = (
        await db.execute(
            select(Server).where(Server.is_active == True).order_by(Server.priority.desc(), Server.name)
        )
    ).scalars().all()

    panels = (
        await db.execute(
            select(Panel).where(Panel.is_active == True).order_by(Panel.name)
        )
    ).scalars().all()

    if not servers and not panels:
        return []

    from urllib.parse import urlparse

    async def ping_server(s: Server):
        host = s.subscription_host
        port = s.subscription_port or 443
        ms = await _measure_tcp_ping(host, port)
        return (s, ms)

    async def ping_panel(p: Panel):
        try:
            parsed = urlparse(p.base_url)
            host = parsed.hostname or p.base_url
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            ms = await _measure_tcp_ping(host, port)
            return (p, ms)
        except Exception:
            return (p, None)

    loc_groups: Dict[str, Dict[str, Any]] = {}

    if servers:
        results = await asyncio.gather(*(ping_server(s) for s in servers))
        for s, ms in results:
            raw_code = s.location_code or "ALL"
            code = raw_code.split("-")[0].upper() if raw_code else "ALL"
            if code not in loc_groups:
                loc_groups[code] = {
                    "location_code": code,
                    "location_name": s.location_name or code,
                    "flag": s.flag_emoji or "🌐",
                    "pings": [],
                    "active_nodes": 0,
                }
            loc_groups[code]["active_nodes"] += 1
            if ms is not None:
                loc_groups[code]["pings"].append(ms)
    elif panels:
        results = await asyncio.gather(*(ping_panel(p) for p in panels))
        for p, ms in results:
            code = f"PNL-{p.name[:4].upper()}"
            loc_groups[code] = {
                "location_code": code,
                "location_name": p.name or "پنل سرور",
                "flag": "🖥",
                "pings": [ms] if ms is not None else [],
                "active_nodes": 1,
            }

    response_items = []
    for code, g in loc_groups.items():
        pings = g["pings"]
        avg_ping = round(sum(pings) / len(pings), 1) if pings else None
        status = "ONLINE" if (pings and len(pings) > 0) else "DEGRADED"
        response_items.append({
            "location_code": g["location_code"],
            "location_name": g["location_name"],
            "flag": g["flag"],
            "status": status,
            "ping_ms": avg_ping,
            "active_nodes": g["active_nodes"],
        })

    return response_items



@router.post("/servers/{server_id}/toggle")
async def toggle_server_status(
    server_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(Server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="سرور یافت نشد.")

    server.is_active = not server.is_active

    # Also toggle corresponding Inbound.is_active
    toggled_inbounds = []
    if getattr(server, "inbound_id", None):
        inb = await db.get(Inbound, server.inbound_id)
        if inb:
            inb.is_active = server.is_active
            toggled_inbounds.append(str(inb.id))
    else:
        # Fallback: check matching inbound on same panel
        from app.modules.servers.allocation import inbound_matches_location
        inbounds = (await db.execute(select(Inbound).where(Inbound.panel_id == server.panel_id))).scalars().all()
        for inb in inbounds:
            if inbound_matches_location(inb.remark, inb.tag, server.location_code, inb.remote_inbound_id):
                inb.is_active = server.is_active
                server.inbound_id = inb.id
                toggled_inbounds.append(str(inb.id))
                break

    await db.commit()
    return {"success": True, "is_active": server.is_active, "toggled_inbounds": toggled_inbounds}


@router.post("/inbounds/{inbound_id}/toggle")
async def toggle_inbound_status(
    inbound_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Toggle inbound active/inactive state in database for bot provisioning."""
    inbound = await db.get(Inbound, inbound_id)
    if not inbound:
        raise HTTPException(status_code=404, detail="اینباند یافت نشد.")

    inbound.is_active = not inbound.is_active

    # Also toggle corresponding Server(s)
    srv_res = await db.execute(select(Server).where(Server.inbound_id == inbound.id))
    servers = list(srv_res.scalars().all())
    if not servers:
        from app.modules.servers.allocation import inbound_matches_location
        all_srvs = (await db.execute(select(Server).where(Server.panel_id == inbound.panel_id))).scalars().all()
        for s in all_srvs:
            if inbound_matches_location(inbound.remark, inbound.tag, s.location_code, inbound.remote_inbound_id):
                s.inbound_id = inbound.id
                servers.append(s)

    for s in servers:
        s.is_active = inbound.is_active

    await db.commit()
    return {"success": True, "is_active": inbound.is_active, "toggled_servers": [str(s.id) for s in servers]}


@router.post("/servers/sync-from-inbounds")
@router.post("/panels/sync-inbounds-to-servers")
async def sync_servers_from_inbounds_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """One-time or manual sync converting all existing DB inbounds into Server entities."""
    from app.modules.servers.allocation import sync_inbounds_to_servers
    panels = (await db.execute(select(Panel))).scalars().all()
    total_synced = 0
    results = []
    for p in panels:
        inbounds = (await db.execute(select(Inbound).where(Inbound.panel_id == p.id))).scalars().all()
        if inbounds:
            synced = await sync_inbounds_to_servers(db, p, inbounds)
            total_synced += len(synced)
            results.append({"panel_id": str(p.id), "panel_name": p.name, "servers_synced": len(synced)})
    await db.commit()
    return {
        "success": True,
        "total_servers_synced": total_synced,
        "panels": results,
        "message": f"همگام‌سازی اینباندها با سرورها با موفقیت انجام شد ({total_synced} سرور همگام گردید).",
    }


@router.get("/locations")
async def list_available_locations(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Retrieve all available active inbounds and server locations directly from connected panels/servers.
    Zero hardcoded locations.
    """
    servers = (
        await db.execute(
            select(Server)
            .where(Server.is_active == True)
            .order_by(Server.priority.desc(), Server.name.asc())
        )
    ).scalars().all()

    inbounds = (await db.execute(select(Inbound))).scalars().all()
    inbound_map = {ib.id: ib for ib in inbounds}

    from app.modules.servers.allocation import LOCATION_TO_INBOUND_ID, COUNTRY_NAME_MAP
    import re

    def _find_best_inbound(loc_code: str):
        loc_u = (loc_code or "").upper()
        country = loc_u.split("-")[0]
        num = loc_u.split("-")[1] if "-" in loc_u else ""
        num_str = f"0{num}" if len(num) == 1 else num

        target_ids = LOCATION_TO_INBOUND_ID.get(loc_u) or LOCATION_TO_INBOUND_ID.get(country, [])
        for tid in target_ids:
            for inb in inbounds:
                if inb.remote_inbound_id == tid:
                    return inb

        aliases = COUNTRY_NAME_MAP.get(country, [])
        if aliases:
            for inb in inbounds:
                r_u = (inb.remark or inb.tag or "").upper()
                if any(a in r_u for a in aliases):
                    return inb

        if num:
            for inb in inbounds:
                r_u = (inb.remark or inb.tag or "").upper()
                if (
                    f"-{country}-{num}" in r_u
                    or f"-{country}-{num_str}" in r_u
                    or (f"-{country}-" in r_u and (f"-{num}" in r_u or f"-{num_str}" in r_u))
                ):
                    return inb

        for inb in inbounds:
            r_u = (inb.remark or inb.tag or "").upper()
            if re.search(rf"(?:^|[\s\-_]){re.escape(country)}(?:[\s\-_]|$|\d+)", r_u):
                return inb

        return None

    locations = []
    seen_codes = set()
    for s in servers:
        matched_inb = None
        if getattr(s, "inbound_id", None):
            matched_inb = inbound_map.get(s.inbound_id)
        if not matched_inb:
            matched_inb = _find_best_inbound(s.location_code)

        if matched_inb and not matched_inb.is_active:
            continue

        inb_info = ""
        inb_id = None
        if matched_inb:
            inb_info = f"اینباند {matched_inb.remote_inbound_id}: {matched_inb.remark or matched_inb.tag} (پورت {matched_inb.port})"
            inb_id = matched_inb.remote_inbound_id

        country = s.location_code.split("-")[0].upper() if s.location_code else "ALL"
        code = (s.location_code or f"SRV-{s.id}").strip().upper()
        if code in seen_codes:
            code = f"{code}_{s.id.hex[:4]}"
        seen_codes.add(code)

        locations.append({
            "code": code,
            "country_code": country,
            "name": s.location_name or country,
            "full_name": s.name,
            "flag": s.flag_emoji or "🌐",
            "inbound_id": inb_id,
            "inbound_info": inb_info,
            "server_id": str(s.id),
            "host": s.subscription_host,
            "port": s.subscription_port,
            "protocol": matched_inb.protocol if matched_inb else "vless",
        })

    return {"locations": locations}


# -------------------------------------------------------------
# 8. Products and Plans Management
# -------------------------------------------------------------
@router.get("/products")
async def list_products(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    products = (await db.execute(select(Product).order_by(Product.sort_order.asc(), Product.price_irr.asc()))).scalars().all()
    items = [
        {
            "id": str(p.id),
            "name": p.name,
            "traffic_gb": p.traffic_gb,
            "duration_days": p.duration_days,
            "price_toman": p.price_toman,
            "protocol": p.protocol,
            "allowed_locations": p.allowed_locations or "ALL",
            "location_mode": getattr(p, "location_mode", "all") or ("all" if (not p.allowed_locations or p.allowed_locations == 'ALL') else "custom"),
            "is_active": p.is_active,
            "sort_order": p.sort_order,
        }
        for p in products
    ]
    return {"items": items}


@router.post("/products")
async def create_product(
    req: ProductCreateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    traffic_bytes = int(req.traffic_gb * (1024**3)) if req.traffic_gb > 0 else 0
    p = Product(
        id=uuid.uuid4(),
        name=req.name.strip(),
        price_irr=req.price_toman * 10,
        traffic_bytes=traffic_bytes,
        duration_days=req.duration_days,
        protocol=(req.protocol or "vless").lower().strip(),
        allowed_locations=(req.allowed_locations or "ALL").strip(),
        location_mode=(req.location_mode or "all").lower().strip(),
        is_active=req.is_active,
        sort_order=req.sort_order,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return {
        "success": True,
        "message": f"تعرفه «{p.name}» با موفقیت افزوده شد.",
        "product": {
            "id": str(p.id),
            "name": p.name,
            "traffic_gb": p.traffic_gb,
            "duration_days": p.duration_days,
            "price_toman": p.price_toman,
            "protocol": p.protocol,
            "allowed_locations": p.allowed_locations,
            "location_mode": p.location_mode,
            "is_active": p.is_active,
            "sort_order": p.sort_order,
        }
    }


@router.post("/products/{product_id}")
async def update_product(
    product_id: uuid.UUID,
    req: ProductUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Product, product_id)
    if not p:
        raise HTTPException(status_code=404, detail="تعرفه مورد نظر یافت نشد.")

    if req.name is not None:
        p.name = req.name.strip()
    if req.price_toman is not None:
        p.price_irr = req.price_toman * 10
    if req.traffic_gb is not None:
        p.traffic_bytes = int(req.traffic_gb * (1024**3)) if req.traffic_gb > 0 else 0
    if req.duration_days is not None:
        p.duration_days = req.duration_days
    if req.protocol is not None:
        p.protocol = req.protocol.lower().strip()
    if req.allowed_locations is not None:
        p.allowed_locations = req.allowed_locations.strip()
    if req.location_mode is not None:
        p.location_mode = req.location_mode.lower().strip()
    if req.is_active is not None:
        p.is_active = req.is_active
    if req.sort_order is not None:
        p.sort_order = req.sort_order

    await db.commit()
    return {"success": True, "message": f"تعرفه «{p.name}» با موفقیت بروزرسانی شد."}


@router.delete("/products/{product_id}")
async def delete_product(
    product_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Product, product_id)
    if not p:
        raise HTTPException(status_code=404, detail="تعرفه مورد نظر یافت نشد.")

    # Check if orders exist for this product
    orders_cnt = (await db.execute(
        select(func.count(Order.id)).where(Order.product_id == product_id)
    )).scalar_one()

    if orders_cnt > 0:
        p.is_active = False
        await db.commit()
        return {
            "success": True,
            "message": f"به دلیل وجود {orders_cnt} سفارش ثبت‌شده با این تعرفه، این پلن غیرفعال شد تا تداخلی ایجاد نشود.",
            "is_active": False,
        }
    else:
        await db.delete(p)
        await db.commit()
        return {"success": True, "message": f"تعرفه «{p.name}» با موفقیت حذف گردید."}
    return {"success": True, "message": "بسته با موفقیت بروزرسانی شد."}


# -------------------------------------------------------------
# 9. System Settings & Broadcast Tool
# -------------------------------------------------------------
@router.get("/system-settings")
async def get_system_settings_list(
    admin: User = Depends(get_current_admin),
):
    """Retrieve all dynamic system settings categorized cleanly (3-to-97 Rule)."""
    from app.services.settings_service import SettingsService
    return await SettingsService.get_all()


@router.get("/settings")
async def get_system_settings(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    inbounds = (await db.execute(select(Inbound).where(Inbound.is_active == True))).scalars().all()
    inb_list = [
        {"id": str(ib.id), "label": f"{ib.remark or ib.protocol} (پورت {ib.port})"}
        for ib in inbounds
    ]

    from app.services.panel_health_service import PanelHealthService
    health_cfg = PanelHealthService.get_monitoring_config()

    return {
        "trial_enabled": getattr(settings, "TRIAL_ENABLED", True),
        "trial_traffic_gb": getattr(settings, "TRIAL_TRAFFIC_MB", 1024) / 1024,
        "trial_duration_days": getattr(settings, "TRIAL_DURATION_DAYS", 1),
        "trial_max_count": getattr(settings, "TRIAL_MAX_COUNT", 1),
        "trial_inbound_id": getattr(settings, "TRIAL_INBOUND_ID", None),
        "card_number": settings.CARD_NUMBER,
        "card_holder": settings.CARD_HOLDER,
        "card_bank_name": settings.CARD_BANK_NAME,
        "card_to_card_enabled": getattr(settings, "CARD_TO_CARD_ENABLED", True),
        "referral_commission_percent": settings.REFERRAL_COMMISSION_PERCENT,
        "health_check_interval": health_cfg["check_interval_seconds"],
        "health_retry_count": health_cfg["retry_count"],
        "health_retry_interval": health_cfg["retry_interval_seconds"],
        "health_alert_cooldown": health_cfg["alert_cooldown_minutes"],
        "inbounds": inb_list,
    }


@router.post("/settings")
async def update_system_settings(
    req: SystemSettingsUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.services.settings_service import SettingsService

    if req.referral_commission_percent is not None:
        settings.REFERRAL_COMMISSION_PERCENT = req.referral_commission_percent
    if req.trial_enabled is not None:
        settings.TRIAL_ENABLED = req.trial_enabled
        update_env_variable("TRIAL_ENABLED", str(req.trial_enabled).lower())

    if req.trial_traffic_mb is not None:
        settings.TRIAL_TRAFFIC_MB = req.trial_traffic_mb
        update_env_variable("TRIAL_TRAFFIC_MB", str(req.trial_traffic_mb))

    if req.trial_duration_days is not None:
        settings.TRIAL_DURATION_DAYS = req.trial_duration_days
        settings.TRIAL_DURATION_HOURS = req.trial_duration_days * 24
        update_env_variable("TRIAL_DURATION_DAYS", str(req.trial_duration_days))

    if req.trial_max_count is not None:
        settings.TRIAL_MAX_COUNT = req.trial_max_count
        update_env_variable("TRIAL_MAX_COUNT", str(req.trial_max_count))

    if req.trial_inbound_id is not None:
        settings.TRIAL_INBOUND_ID = req.trial_inbound_id if req.trial_inbound_id != "auto" else None
        update_env_variable("TRIAL_INBOUND_ID", settings.TRIAL_INBOUND_ID or "")

    if req.card_number is not None:
        settings.CARD_NUMBER = req.card_number
        update_env_variable("CARD_NUMBER", req.card_number)

    if req.card_holder is not None:
        settings.CARD_HOLDER = req.card_holder
        update_env_variable("CARD_HOLDER", req.card_holder)

    if req.card_bank_name is not None:
        settings.CARD_BANK_NAME = req.card_bank_name
        update_env_variable("CARD_BANK_NAME", req.card_bank_name)

    if req.card_to_card_enabled is not None:
        settings.CARD_TO_CARD_ENABLED = req.card_to_card_enabled
        update_env_variable("CARD_TO_CARD_ENABLED", str(req.card_to_card_enabled).lower())

    # Health monitoring settings
    if req.health_check_interval is not None:
        await SettingsService.set_value(
            "PANEL_HEALTH_CHECK_INTERVAL",
            str(req.health_check_interval),
            category="monitoring",
            description="بازه زمانی بررسی دوره‌ای سلامت پنل‌ها (به ثانیه)",
            session=db,
        )

    if req.health_retry_count is not None:
        await SettingsService.set_value(
            "PANEL_HEALTH_RETRY_COUNT",
            str(req.health_retry_count),
            category="monitoring",
            description="تعداد دفعات تست مجدد متوالی قبل از ارسال هشدار خطا به تلگرام",
            session=db,
        )

    if req.health_retry_interval is not None:
        await SettingsService.set_value(
            "PANEL_HEALTH_RETRY_INTERVAL",
            str(req.health_retry_interval),
            category="monitoring",
            description="فاصله زمانی بین هر تلاش مجدد (به ثانیه)",
            session=db,
        )

    if req.health_alert_cooldown is not None:
        await SettingsService.set_value(
            "PANEL_HEALTH_ALERT_COOLDOWN",
            str(req.health_alert_cooldown),
            category="monitoring",
            description="حداقل فاصله زمانی بین ارسال هشدارهای تکراری خطا (به دقیقه)",
            session=db,
        )

    await db.commit()
    return {"success": True, "message": "تنظیمات با موفقیت ذخیره شدند."}


@router.get("/settings/health-monitor")
async def get_health_monitor_settings_endpoint(
    admin: User = Depends(get_current_admin),
):
    """Retrieve panel health check & Xray monitoring configuration."""
    from app.services.panel_health_service import PanelHealthService
    return PanelHealthService.get_monitoring_config()


@router.post("/settings/health-monitor")
async def update_health_monitor_settings_endpoint(
    req: HealthMonitorSettingsRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update panel health check & Xray monitoring configuration."""
    from app.services.settings_service import SettingsService

    if req.check_interval_seconds is not None:
        await SettingsService.set_value(
            "PANEL_HEALTH_CHECK_INTERVAL",
            str(req.check_interval_seconds),
            category="monitoring",
            description="بازه زمانی بررسی دوره‌ای سلامت پنل‌ها (به ثانیه)",
            session=db,
        )

    if req.retry_count is not None:
        await SettingsService.set_value(
            "PANEL_HEALTH_RETRY_COUNT",
            str(req.retry_count),
            category="monitoring",
            description="تعداد دفعات تست مجدد متوالی قبل از ارسال هشدار خطا به تلگرام",
            session=db,
        )

    if req.retry_interval_seconds is not None:
        await SettingsService.set_value(
            "PANEL_HEALTH_RETRY_INTERVAL",
            str(req.retry_interval_seconds),
            category="monitoring",
            description="فاصله زمانی بین هر تلاش مجدد (به ثانیه)",
            session=db,
        )

    if req.alert_cooldown_minutes is not None:
        await SettingsService.set_value(
            "PANEL_HEALTH_ALERT_COOLDOWN",
            str(req.alert_cooldown_minutes),
            category="monitoring",
            description="حداقل فاصله زمانی بین ارسال هشدارهای تکراری خطا (به دقیقه)",
            session=db,
        )

    await db.commit()
    from app.services.panel_health_service import PanelHealthService
    cfg = PanelHealthService.get_monitoring_config()
    return {
        "success": True,
        "message": "تنظیمات مانیتورینگ سلامت و هشدارهای Xray با موفقیت ذخیره شد.",
        "config": cfg,
    }


@router.get("/settings/payment-details")
async def get_payment_details_settings(
    admin: User = Depends(get_current_admin),
):
    """Fetch live card, crypto, and Zarinpal payment gateway configurations."""
    c2c_raw = get_live_env("CARD_TO_CARD_ENABLED", "false")
    c2c_enabled = str(c2c_raw).lower() in ("true", "1", "yes", "on")

    crypto_raw = get_live_env("CRYPTO_ENABLED", "false")
    crypto_enabled = str(crypto_raw).lower() in ("true", "1", "yes", "on")

    zarinpal_raw = get_live_env("ZARINPAL_ENABLED", "false")
    zarinpal_enabled = str(zarinpal_raw).lower() in ("true", "1", "yes", "on")

    wallet_trc20 = str(
        get_live_env("CRYPTO_WALLET_TRC20", "")
        or get_live_env("CRYPTO_WALLET_USDT", getattr(settings, "CRYPTO_WALLET_TRC20", ""))
    )

    trc20_raw = get_live_env("CRYPTO_TRC20_ENABLED", "false")
    trc20_enabled = str(trc20_raw).lower() in ("true", "1", "yes", "on")

    bep20_raw = get_live_env("CRYPTO_BEP20_ENABLED", "false")
    bep20_enabled = str(bep20_raw).lower() in ("true", "1", "yes", "on")

    wallet_bep20 = str(
        get_live_env("CRYPTO_WALLET_BEP20", getattr(settings, "CRYPTO_WALLET_BEP20", "0x587EF4ea392c6947688533fb0550Bae94B8f0E8a"))
    )

    try:
        usdt_rate = int(get_live_env("USDT_TOMAN_RATE", str(getattr(settings, "USDT_TOMAN_RATE", 70000))))
    except Exception:
        usdt_rate = 70000

    try:
        min_topup = int(get_live_env("MIN_WALLET_TOPUP_TOMAN", str(getattr(settings, "MIN_WALLET_TOPUP_TOMAN", 10000))))
    except Exception:
        min_topup = 10000

    merchant_id = str(
        get_live_env("ZARINPAL_MERCHANT_ID", getattr(settings, "ZARINPAL_MERCHANT_ID", "00000000-0000-0000-0000-000000000000"))
    )

    stripe_raw = get_live_env("STRIPE_ENABLED", "false")
    stripe_enabled = str(stripe_raw).lower() in ("true", "1", "yes", "on")
    stripe_pub = str(get_live_env("STRIPE_PUBLISHABLE_KEY", getattr(settings, "STRIPE_PUBLISHABLE_KEY", "")))
    stripe_sec = str(get_live_env("STRIPE_SECRET_KEY", getattr(settings, "STRIPE_SECRET_KEY", "")))
    stripe_curr = str(get_live_env("STRIPE_CURRENCY", getattr(settings, "STRIPE_CURRENCY", "USD"))).upper()
    try:
        stripe_rate = int(get_live_env("STRIPE_EXCHANGE_RATE", str(getattr(settings, "STRIPE_EXCHANGE_RATE", 70000))))
    except Exception:
        stripe_rate = 70000

    return {
        "card_number": str(get_live_env("CARD_NUMBER", "6037997100000000")),
        "card_holder": str(get_live_env("CARD_HOLDER", "مدیر سیستم")),
        "card_bank_name": str(get_live_env("CARD_BANK_NAME", "بانک ملی ایران")),
        "card_iban": str(get_live_env("CARD_IBAN", "IR000000000000000000000000")),
        "crypto_wallet_usdt": wallet_trc20,
        "crypto_wallet_trc20": wallet_trc20,
        "crypto_wallet_bep20": wallet_bep20,
        "card_to_card_enabled": c2c_enabled,
        "crypto_enabled": crypto_enabled,
        "crypto_trc20_enabled": trc20_enabled,
        "crypto_bep20_enabled": bep20_enabled,
        "usdt_toman_rate": usdt_rate,
        "zarinpal_enabled": zarinpal_enabled,
        "zarinpal_merchant_id": merchant_id,
        "stripe_enabled": stripe_enabled,
        "stripe_publishable_key": stripe_pub,
        "stripe_secret_key": stripe_sec,
        "stripe_currency": stripe_curr,
        "stripe_exchange_rate": stripe_rate,
        "min_wallet_topup_toman": min_topup,
    }


@router.post("/settings/payment-details")
async def update_payment_details_settings(
    req: PaymentDetailsUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update card, crypto, and Zarinpal payment credentials with live process synchronization."""
    updated = {}
    if req.card_number is not None:
        val = req.card_number.strip()
        update_env_variable("CARD_NUMBER", val)
        settings.CARD_NUMBER = val
        updated["card_number"] = val
    if req.card_holder is not None:
        val = req.card_holder.strip()
        update_env_variable("CARD_HOLDER", val)
        settings.CARD_HOLDER = val
        updated["card_holder"] = val
    if req.card_bank_name is not None:
        val = req.card_bank_name.strip()
        update_env_variable("CARD_BANK_NAME", val)
        settings.CARD_BANK_NAME = val
        updated["card_bank_name"] = val
    if req.card_iban is not None:
        val = req.card_iban.strip()
        update_env_variable("CARD_IBAN", val)
        settings.CARD_IBAN = val
        updated["card_iban"] = val
    if req.crypto_wallet_trc20 is not None:
        val = req.crypto_wallet_trc20.strip()
        update_env_variable("CRYPTO_WALLET_TRC20", val)
        update_env_variable("CRYPTO_WALLET_USDT", val)
        settings.CRYPTO_WALLET_TRC20 = val
        settings.CRYPTO_WALLET_USDT = val
        updated["crypto_wallet_trc20"] = val
    elif req.crypto_wallet_usdt is not None:
        val = req.crypto_wallet_usdt.strip()
        update_env_variable("CRYPTO_WALLET_TRC20", val)
        update_env_variable("CRYPTO_WALLET_USDT", val)
        settings.CRYPTO_WALLET_TRC20 = val
        settings.CRYPTO_WALLET_USDT = val
        updated["crypto_wallet_usdt"] = val
    if req.crypto_wallet_bep20 is not None:
        val = req.crypto_wallet_bep20.strip()
        update_env_variable("CRYPTO_WALLET_BEP20", val)
        settings.CRYPTO_WALLET_BEP20 = val
        updated["crypto_wallet_bep20"] = val
    if req.crypto_trc20_enabled is not None:
        update_env_variable("CRYPTO_TRC20_ENABLED", str(req.crypto_trc20_enabled).lower())
        settings.CRYPTO_TRC20_ENABLED = req.crypto_trc20_enabled
        updated["crypto_trc20_enabled"] = req.crypto_trc20_enabled
    if req.crypto_bep20_enabled is not None:
        update_env_variable("CRYPTO_BEP20_ENABLED", str(req.crypto_bep20_enabled).lower())
        settings.CRYPTO_BEP20_ENABLED = req.crypto_bep20_enabled
        updated["crypto_bep20_enabled"] = req.crypto_bep20_enabled
    if req.card_to_card_enabled is not None:
        update_env_variable("CARD_TO_CARD_ENABLED", str(req.card_to_card_enabled).lower())
        settings.CARD_TO_CARD_ENABLED = req.card_to_card_enabled
        updated["card_to_card_enabled"] = req.card_to_card_enabled
    if req.crypto_enabled is not None:
        update_env_variable("CRYPTO_ENABLED", str(req.crypto_enabled).lower())
        settings.CRYPTO_ENABLED = req.crypto_enabled
        updated["crypto_enabled"] = req.crypto_enabled
    if req.usdt_toman_rate is not None:
        update_env_variable("USDT_TOMAN_RATE", str(req.usdt_toman_rate))
        settings.USDT_TOMAN_RATE = req.usdt_toman_rate
        updated["usdt_toman_rate"] = req.usdt_toman_rate
    if req.zarinpal_enabled is not None:
        update_env_variable("ZARINPAL_ENABLED", str(req.zarinpal_enabled).lower())
        settings.ZARINPAL_ENABLED = req.zarinpal_enabled
        updated["zarinpal_enabled"] = req.zarinpal_enabled
    if req.zarinpal_merchant_id is not None:
        val = req.zarinpal_merchant_id.strip()
        update_env_variable("ZARINPAL_MERCHANT_ID", val)
        settings.ZARINPAL_MERCHANT_ID = val
        updated["zarinpal_merchant_id"] = val
    if req.stripe_enabled is not None:
        update_env_variable("STRIPE_ENABLED", str(req.stripe_enabled).lower())
        settings.STRIPE_ENABLED = req.stripe_enabled
        updated["stripe_enabled"] = req.stripe_enabled
    if req.stripe_publishable_key is not None:
        val = req.stripe_publishable_key.strip()
        update_env_variable("STRIPE_PUBLISHABLE_KEY", val)
        settings.STRIPE_PUBLISHABLE_KEY = val
        updated["stripe_publishable_key"] = val
    if req.stripe_secret_key is not None:
        val = req.stripe_secret_key.strip()
        update_env_variable("STRIPE_SECRET_KEY", val)
        settings.STRIPE_SECRET_KEY = val
        updated["stripe_secret_key"] = val
    if req.stripe_currency is not None:
        val = req.stripe_currency.strip().upper()
        update_env_variable("STRIPE_CURRENCY", val)
        settings.STRIPE_CURRENCY = val
        updated["stripe_currency"] = val
    if req.stripe_exchange_rate is not None:
        update_env_variable("STRIPE_EXCHANGE_RATE", str(req.stripe_exchange_rate))
        settings.STRIPE_EXCHANGE_RATE = req.stripe_exchange_rate
        updated["stripe_exchange_rate"] = req.stripe_exchange_rate
    if req.min_wallet_topup_toman is not None:
        update_env_variable("MIN_WALLET_TOPUP_TOMAN", str(req.min_wallet_topup_toman))
        settings.MIN_WALLET_TOPUP_TOMAN = req.min_wallet_topup_toman
        updated["min_wallet_topup_toman"] = req.min_wallet_topup_toman

    await record_audit(
        db,
        action="PAYMENT_DETAILS_UPDATED",
        details=f"Updated payment settings: {list(updated.keys())}",
        actor_id=admin.telegram_id,
    )
    await db.commit()

    return {
        "success": True,
        "message": "اطلاعات حساب و درگاه پرداخت با موفقیت بروزرسانی شد.",
        "settings": await get_payment_details_settings(admin=admin),
    }


@router.get("/settings/security")
async def get_admin_security_settings(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Fetch live web panel credentials and 2FA settings."""
    admins_res = await db.execute(
        select(User).where(User.role.in_([UserRole.ADMIN, UserRole.SUPER_ADMIN, "super_admin", "admin"]))
    )
    db_admins = admins_res.scalars().all()
    admin_list = []
    seen_ids = set()
    for a in db_admins:
        if a.telegram_id:
            label = f"{a.first_name or ''} (@{a.username})" if a.username else (a.first_name or f"ID: {a.telegram_id}")
            admin_list.append({"telegram_id": a.telegram_id, "label": label.strip(), "username": a.username or ""})
            seen_ids.add(a.telegram_id)
    for tid in getattr(settings, "ADMIN_TELEGRAM_IDS", []):
        if tid not in seen_ids:
            alias = "مدیر سیستم"
            admin_list.append({"telegram_id": tid, "label": f"{alias} ({tid})", "username": alias})
            seen_ids.add(tid)

    return {
        "admin_web_username": settings.ADMIN_WEB_USERNAME,
        "admin_2fa_enabled": settings.ADMIN_2FA_ENABLED,
        "admin_2fa_telegram_id": settings.ADMIN_2FA_TELEGRAM_ID,
        "available_admins": admin_list,
    }


@router.post("/settings/security")
async def update_admin_security_settings(
    req: AdminSecurityUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update web admin username, password, 2FA toggle, and target Telegram ID with password validation."""
    # Fix 2.2: Verify current_password before allowing security or credential changes
    cur_pw = (req.current_password or "").strip()
    is_valid_pw = False
    if settings.ADMIN_WEB_PASSWORD and cur_pw == settings.ADMIN_WEB_PASSWORD:
        is_valid_pw = True
    else:
        try:
            if settings.ADMIN_WEB_PASSWORD_HASH and bcrypt.checkpw(cur_pw.encode("utf-8"), settings.ADMIN_WEB_PASSWORD_HASH.encode("utf-8")):
                is_valid_pw = True
        except Exception:
            pass

    if not is_valid_pw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="رمز عبور فعلی مدیریت نامعتبر است. جهت اعمال تغییرات امنیتی، ورود رمز عبور صحیح الزامی می‌باشد.",
        )

    updated = []
    password_changed = False

    if req.admin_web_username is not None and req.admin_web_username.strip():
        new_user = req.admin_web_username.strip()
        settings.ADMIN_WEB_USERNAME = new_user
        update_env_variable("ADMIN_WEB_USERNAME", new_user)
        from app.services.settings_service import SettingsService
        await SettingsService.set_value("ADMIN_WEB_USERNAME", new_user, session=db)
        if admin:
            admin.username = new_user
        updated.append(f"username={new_user}")

    if req.admin_web_password is not None and req.admin_web_password.strip():
        new_pw = req.admin_web_password.strip()
        settings.ADMIN_WEB_PASSWORD = new_pw
        update_env_variable("ADMIN_WEB_PASSWORD", new_pw)
        new_hash = bcrypt.hashpw(new_pw.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")
        update_env_variable("ADMIN_WEB_PASSWORD_HASH", new_hash)
        from app.services.settings_service import SettingsService
        await SettingsService.set_value("ADMIN_WEB_PASSWORD", new_pw, session=db)
        await SettingsService.set_value("ADMIN_WEB_PASSWORD_HASH", new_hash, session=db)
        updated.append("password=******")
        password_changed = True

    if req.admin_2fa_enabled is not None:
        settings.ADMIN_2FA_ENABLED = req.admin_2fa_enabled
        update_env_variable("ADMIN_2FA_ENABLED", str(req.admin_2fa_enabled).lower())
        updated.append(f"2fa_enabled={req.admin_2fa_enabled}")

    if req.admin_2fa_telegram_id is not None:
        settings.ADMIN_2FA_TELEGRAM_ID = req.admin_2fa_telegram_id
        update_env_variable("ADMIN_2FA_TELEGRAM_ID", str(req.admin_2fa_telegram_id))
        cur_ids = list(getattr(settings, "ADMIN_TELEGRAM_IDS", []))
        if req.admin_2fa_telegram_id not in cur_ids:
            cur_ids.append(req.admin_2fa_telegram_id)
            settings.ADMIN_TELEGRAM_IDS = cur_ids
            update_env_variable("ADMIN_TELEGRAM_IDS", ",".join(str(x) for x in cur_ids))
        updated.append(f"2fa_telegram_id={req.admin_2fa_telegram_id}")

    # Fix 2.3: Invalidate all existing tokens if password changed
    if password_changed:
        await revoke_all_tokens()

    await record_audit(
        db,
        action="ADMIN_SECURITY_UPDATED",
        details=f"Updated web admin credentials & security: {updated}",
        actor_id=admin.telegram_id,
    )
    await db.commit()

    new_token = None
    if not password_changed:
        new_token = create_admin_token(
            telegram_id=getattr(admin, "telegram_id", 0) or 0,
            username=settings.ADMIN_WEB_USERNAME,
            role=getattr(admin, "role", "super_admin") or "super_admin",
        )

    return {
        "success": True,
        "message": "تنظیمات ورود و امنیت پنل وب با موفقیت ذخیره شدند." + (" (تمام نشست‌های دیگر منقضی شدند)" if password_changed else ""),
        "token": new_token,
        "new_username": settings.ADMIN_WEB_USERNAME,
        "password_changed": password_changed,
        "settings": {
            "admin_web_username": settings.ADMIN_WEB_USERNAME,
            "admin_2fa_enabled": settings.ADMIN_2FA_ENABLED,
            "admin_2fa_telegram_id": settings.ADMIN_2FA_TELEGRAM_ID,
        }
    }


@router.post("/settings/change-username")
async def change_admin_username(
    req: ChangeUsernameRequest,
    response: Response,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Dedicated endpoint to change the webpanel admin username cleanly and immediately.
    Updates in-memory settings, .env, system_settings database table, and the active User entity.
    Issues a fresh authenticated session token with the new username.
    """
    new_user = req.new_username.strip()
    if not new_user:
        raise HTTPException(status_code=400, detail="نام کاربری نمی‌تواند خالی باشد.")

    if len(new_user) < 3:
        raise HTTPException(status_code=400, detail="نام کاربری باید حداقل ۳ کاراکتر باشد.")

    import re
    if not re.match(r"^[a-zA-Z0-9_\-\.@]+$", new_user):
        raise HTTPException(status_code=400, detail="نام کاربری فقط می‌تواند شامل حروف انگلیسی، اعداد، خط فاصله و زیرخط باشد.")

    # If current_password is provided, verify it
    if req.current_password:
        cur_p = req.current_password.strip()
        expected_pw = settings.ADMIN_WEB_PASSWORD
        is_valid = (expected_pw and cur_p == expected_pw)
        if not is_valid and settings.ADMIN_WEB_PASSWORD_HASH:
            try:
                import bcrypt
                is_valid = bcrypt.checkpw(cur_p.encode("utf-8"), settings.ADMIN_WEB_PASSWORD_HASH.encode("utf-8"))
            except Exception:
                pass
        if not is_valid:
            raise HTTPException(status_code=401, detail="رمز عبور فعلی وارد شده نادرست است.")

    old_user = settings.ADMIN_WEB_USERNAME

    # 1. Update settings & .env
    settings.ADMIN_WEB_USERNAME = new_user
    update_env_variable("ADMIN_WEB_USERNAME", new_user)

    # 2. Update Database user records for admin
    if admin:
        admin.username = new_user
        db.add(admin)

    # Also update any other admin/super_admin users in DB
    other_admins = (await db.execute(
        select(User).where(User.role.in_([UserRole.ADMIN, UserRole.SUPER_ADMIN]))
    )).scalars().all()
    for u in other_admins:
        u.username = new_user
        db.add(u)

    await record_audit(
        db,
        action="CHANGE_ADMIN_USERNAME",
        details=f"Admin username changed from '{old_user}' to '{new_user}'",
        actor_id=admin.telegram_id,
        target_type="User",
        target_id=str(admin.id),
    )
    await db.commit()
    if admin:
        await db.refresh(admin)

    # 3. Issue fresh session token
    tg_id = admin.telegram_id or (settings.ADMIN_TELEGRAM_IDS[0] if settings.ADMIN_TELEGRAM_IDS else 123456789)
    fresh_token = create_admin_token(
        telegram_id=tg_id,
        username=new_user,
        role=admin.role if hasattr(admin, "role") and admin.role else "super_admin",
        expires_in_seconds=86400 * 7,
    )

    response.set_cookie(
        key="admin_token",
        value=fresh_token,
        max_age=86400 * 7,
        httponly=True,
        samesite="lax",
        secure=False,
    )

    return {
        "success": True,
        "message": f"نام کاربری پنل ادمین با موفقیت به «{new_user}» تغییر یافت.",
        "username": new_user,
        "token": fresh_token,
    }


async def _run_broadcast_background(users: List[int], announcement_text: str, audience: str, actor_id: int):
    """Asynchronous background broadcaster with strict rate limiting to avoid Telegram 429."""
    bot = get_telegram_bot()
    success_count = 0
    fail_count = 0
    try:
        for tg_id in users:
            try:
                await bot.send_message(chat_id=tg_id, text=announcement_text, parse_mode="HTML")
                success_count += 1
            except Exception as e:
                fail_count += 1
                logger.warning(f"Broadcast failed for user {tg_id}: {e}")
            await asyncio.sleep(0.045)  # Rate limit: max ~22 messages per second
    finally:
        await bot.session.close()
    logger.info(f"Broadcast finished. Target: {audience}, Sent: {success_count}/{len(users)}, Failed: {fail_count}")


@router.post("/broadcast")
async def broadcast_announcement(
    req: BroadcastRequest,
    background_tasks: BackgroundTasks = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Queue announcement message broadcast to targeted Telegram bot users in the background."""
    now = datetime.now(timezone.utc)
    audience = (req.target_audience or "all").lower().strip()

    if audience == "active_subs":
        q = (
            select(User.telegram_id)
            .distinct()
            .join(Subscription, Subscription.user_id == User.id)
            .where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
                    User.telegram_id.isnot(None),
                )
            )
        )
    elif audience == "expired_subs":
        q = (
            select(User.telegram_id)
            .distinct()
            .join(Subscription, Subscription.user_id == User.id)
            .where(
                and_(
                    or_(
                        Subscription.status != SubscriptionStatus.ACTIVE,
                        and_(Subscription.expires_at.isnot(None), Subscription.expires_at <= now),
                    ),
                    User.telegram_id.isnot(None),
                )
            )
        )
    else:
        q = select(User.telegram_id).distinct().where(User.telegram_id.isnot(None))

    users = (await db.execute(q)).scalars().all()
    target_ids = [int(u) for u in users if u]

    announcement_text = (
        f"📢 <b>اطلاعیه رسمی مدیریت VPN Private:</b>\n\n"
        f"{req.message}"
    )

    if background_tasks is not None:
        background_tasks.add_task(_run_broadcast_background, target_ids, announcement_text, audience, admin.telegram_id)
    else:
        asyncio.create_task(_run_broadcast_background(target_ids, announcement_text, audience, admin.telegram_id))

    await record_audit(
        db,
        action="BROADCAST_QUEUED",
        details=f"Target: {audience}, Queued for {len(target_ids)} users",
        actor_id=admin.telegram_id,
    )
    await db.commit()

    return {
        "success": True,
        "message": f"ارسال پیام همگانی برای مخاطبان '{audience}' در پس‌زمینه با رعایت محدودیت سرعت تلگرام آغاز شد.",
        "sent_count": len(target_ids),
        "failed_count": 0,
        "total_targets": len(target_ids),
        "target_audience": audience,
    }



# -------------------------------------------------------------
# 10. Telegram Bot Admins Management
# -------------------------------------------------------------
@router.get("/bot-admins")
async def list_bot_admins(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all Telegram Bot admins from config and database."""
    static_ids = getattr(settings, "ADMIN_TELEGRAM_IDS", [])
    if isinstance(static_ids, int):
        static_ids = [static_ids]
    elif not isinstance(static_ids, list):
        static_ids = list(static_ids)

    primary_id = static_ids[0] if static_ids else None

    db_admins_res = await db.execute(
        select(User).where(User.role.in_([UserRole.SUPER_ADMIN, UserRole.ADMIN]))
    )
    db_admins = list(db_admins_res.scalars().all())

    admin_map = {}
    for aid in static_ids:
        if isinstance(aid, int) and aid > 0:
            is_pri = (primary_id is not None and aid == primary_id)
            admin_map[aid] = {
                "telegram_id": aid,
                "username": None,
                "first_name": "مدیر اصلی" if is_pri else "مدیر سیستم",
                "role": "super_admin" if is_pri else "admin",
                "is_primary": is_pri,
                "can_delete": not is_pri,
            }

    for da in db_admins:
        is_pri = (primary_id is not None and da.telegram_id == primary_id)
        role_str = da.role.value if hasattr(da.role, "value") else str(da.role)
        admin_map[da.telegram_id] = {
            "telegram_id": da.telegram_id,
            "username": da.username,
            "first_name": da.first_name or ("مدیر اصلی" if is_pri else "کاربر تلگرام"),
            "role": "super_admin" if is_pri else role_str,
            "is_primary": is_pri,
            "can_delete": not is_pri,
        }

    return {"items": list(admin_map.values())}


@router.post("/bot-admins")
async def add_bot_admin(
    req: AddBotAdminRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Promote a user to Telegram Bot Admin by Telegram ID or @username."""
    ident = req.identifier.strip()
    if not ident:
        raise HTTPException(status_code=400, detail="لطفاً آیدی عددی یا نام‌کاربری تلگرام را وارد کنید.")

    target_tg_id = None
    target_user = None

    if ident.isdigit():
        target_tg_id = int(ident)
        user_res = await db.execute(select(User).where(User.telegram_id == target_tg_id))
        target_user = user_res.scalar_one_or_none()
    else:
        clean_user = ident.lstrip("@")
        user_res = await db.execute(select(User).where(func.lower(User.username) == clean_user.lower()))
        target_user = user_res.scalar_one_or_none()
        if target_user:
            target_tg_id = target_user.telegram_id
        else:
            raise HTTPException(
                status_code=404,
                detail=f"کاربری با نام‌کاربری @{clean_user} در سیستم ثبت‌نام نکرده است. لطفاً آیدی عددی تلگرام را وارد نمایید.",
            )

    if not target_tg_id or target_tg_id <= 0:
        raise HTTPException(status_code=400, detail="شناسه تلگرام نامعتبر است.")

    # 1. Update DB user
    if target_user:
        target_user.role = UserRole.ADMIN
    else:
        target_user = User(
            telegram_id=target_tg_id,
            first_name=f"Admin {target_tg_id}",
            username=None,
            role=UserRole.ADMIN,
        )
        db.add(target_user)

    await db.commit()

    # 2. Add to settings.ADMIN_TELEGRAM_IDS & sync to .env
    cur_ids = list(getattr(settings, "ADMIN_TELEGRAM_IDS", []))
    if target_tg_id not in cur_ids:
        cur_ids.append(target_tg_id)
        settings.ADMIN_TELEGRAM_IDS = cur_ids
        ids_str = ",".join(str(x) for x in cur_ids)
        update_env_variable("ADMIN_TELEGRAM_IDS", ids_str)

    # 3. Notify user via Telegram Bot
    try:
        bot = get_telegram_bot()
        await bot.send_message(
            chat_id=target_tg_id,
            text=(
                "👑 <b>تبریک! دسترسی شما به پنل مدیریت ربات تلگرام فعال شد.</b>\n\n"
                "از این پس می‌توانید با ارسال دستور /admin به تمام بخش‌های مدیریت، تایید فیش‌ها و کانفیگ‌ها دسترسی داشته باشید."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not send promotion message to {target_tg_id}: {e}")

    return {
        "success": True,
        "message": f"کاربر با شناسه {target_tg_id} با موفقیت به مدیران ربات اضافه شد.",
    }


@router.delete("/bot-admins/{telegram_id}")
async def remove_bot_admin(
    telegram_id: int,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Demote a Telegram Bot Admin to customer and remove from admin IDs."""
    # 1. Check if trying to remove the primary admin from ADMIN_TELEGRAM_IDS
    cur_ids = list(getattr(settings, "ADMIN_TELEGRAM_IDS", []))
    primary_id = cur_ids[0] if cur_ids else None
    if primary_id is not None and telegram_id == primary_id:
        raise HTTPException(status_code=400, detail=f"مدیر اصلی سیستم ({telegram_id}) قابل عزل نمی‌باشد.")

    # 2. Remove from settings.ADMIN_TELEGRAM_IDS & sync .env
    if telegram_id in cur_ids:
        cur_ids = [x for x in cur_ids if x != telegram_id]
        settings.ADMIN_TELEGRAM_IDS = cur_ids
        ids_str = ",".join(str(x) for x in cur_ids)
        update_env_variable("ADMIN_TELEGRAM_IDS", ids_str)

    # 2. Demote role in DB
    user_res = await db.execute(select(User).where(User.telegram_id == telegram_id))
    target_user = user_res.scalar_one_or_none()
    if target_user:
        target_user.role = UserRole.CUSTOMER
        await db.commit()

    # 3. Notify user via Bot
    try:
        bot = get_telegram_bot()
        await bot.send_message(
            chat_id=telegram_id,
            text="ℹ️ دسترسی شما به پنل مدیریت ربات توسط مدیر سیستم لغو گردید.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    return {
        "success": True,
        "message": f"دسترسی ادمینی کاربر با شناسه {telegram_id} با موفقیت لغو شد.",
    }


# -------------------------------------------------------------
# 11. PIA WireGuard Dynamic Sync
# -------------------------------------------------------------
class PiaLoginRequest(BaseModel):
    username: str = Field(..., description="PIA username (e.g. p1234567)")
    password: str = Field(..., description="PIA password")


@router.post("/pia/login")
async def login_pia_account(
    req: PiaLoginRequest,
    admin: User = Depends(get_current_admin),
):
    """Authenticate and save PIA account credentials."""
    from app.modules.pia.service import PiaSyncService
    return PiaSyncService.login(username=req.username, password=req.password)


@router.post("/pia/sync")
async def sync_pia_wireguard_nodes(
    admin: User = Depends(get_current_admin),
):
    """Trigger async background sync of PIA WireGuard nodes."""
    from app.modules.pia.service import PiaSyncService
    return PiaSyncService.start_background_sync()


@router.get("/pia/sync/status")
async def get_pia_sync_status(
    admin: User = Depends(get_current_admin),
):
    """Get live status and result of PIA WireGuard sync."""
    from app.modules.pia.service import PiaSyncService
    st = dict(PiaSyncService.get_status())
    u, _ = PiaSyncService.get_credentials()
    st["configured_username"] = u or ""
    return st


# -------------------------------------------------------------
# 12. 3X-UI Database Backup & Restore Management
# -------------------------------------------------------------
@router.post("/xui/backup")
async def trigger_xui_manual_backup(
    admin: User = Depends(get_current_admin),
):
    """Manually triggers an X-UI database backup."""
    from app.modules.xui_backup.service import create_xui_backup
    backup_file = create_xui_backup(action_name="manual")
    if not backup_file:
        raise HTTPException(
            status_code=500,
            detail="ایجاد نسخه پشتیبان با خطا مواجه شد یا فایل دیتابیس 3X-UI یافت نشد.",
        )
    return {
        "success": True,
        "filename": backup_file,
        "message": f"نسخه پشتیبان دیتابیس 3X-UI با موفقیت ایجاد شد: {backup_file}",
    }


@router.get("/xui/backups")
async def get_xui_backups_list(
    admin: User = Depends(get_current_admin),
):
    """Returns list of existing 3X-UI backups."""
    from app.modules.xui_backup.service import list_xui_backups
    backups = list_xui_backups()
    return {
        "success": True,
        "total": len(backups),
        "items": backups,
    }


@router.get("/xui/backups/{filename}/download")
async def download_xui_backup(
    filename: str,
    admin: User = Depends(get_current_admin),
):
    """Download a specific 3X-UI SQLite backup file securely."""
    from app.modules.xui_backup.service import get_backup_dir
    clean_name = filename.strip()
    if (
        not clean_name
        or ".." in clean_name
        or "/" in clean_name
        or "\\" in clean_name
    ):
        raise HTTPException(status_code=400, detail="نام فایل نامعتبر است.")

    target_file = None
    search_dirs = [
        get_backup_dir(),
        Path("/app/data/backups/xui"),
        Path("/opt/vpn-private/data/backups/xui"),
        Path("/opt/vpn-private/backups/xui"),
        Path("./data/backups/xui"),
    ]
    for b_dir in search_dirs:
        try:
            candidate = (b_dir.resolve() / clean_name).resolve()
            if candidate.is_relative_to(b_dir.resolve()) and candidate.is_file():
                target_file = candidate
                break
        except Exception:
            continue

    if not target_file or not target_file.is_file():
        raise HTTPException(status_code=404, detail="فایل پشتیبان مورد نظر یافت نشد.")

    return FileResponse(
        path=str(target_file),
        filename=clean_name,
        media_type="application/octet-stream",
    )


@router.post("/xui/restore")
async def restore_xui_db_backup(
    req: RestoreBackupRequest,
    admin: User = Depends(get_current_admin),
):
    """Restores a specific X-UI backup by filename and restarts container 3x."""
    from app.modules.xui_backup.service import restore_xui_backup
    if not req.filename or not req.filename.strip():
        raise HTTPException(status_code=400, detail="نام فایل پشتیبان الزامی است.")

    success = restore_xui_backup(req.filename.strip())
    if not success:
        raise HTTPException(
            status_code=400,
            detail="بازیابی دیتابیس با خطا مواجه شد. لطفاً نام فایل و دسترسی‌ها را بررسی نمایید.",
        )

    return {
        "success": True,
        "message": f"دیتابیس 3X-UI با موفقیت از فایل {req.filename} بازیابی شد و سرویس 3X-UI مجدداً راه‌اندازی گردید.",
    }


# -------------------------------------------------------------
# 13. Coupons & Promo Codes Management
# -------------------------------------------------------------
@router.get("/coupons")
async def list_coupons(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all coupons and promotional codes."""
    coupons = (
        await db.execute(select(Coupon).order_by(Coupon.created_at.desc()))
    ).scalars().all()

    items = [
        {
            "id": str(c.id),
            "code": c.code,
            "discount_percent": c.discount_percent,
            "discount_amount_toman": c.discount_amount_irr // 10 if c.discount_amount_irr else 0,
            "max_uses": c.max_uses,
            "current_uses": c.current_uses,
            "valid_until": c.valid_until.isoformat() if c.valid_until else None,
            "is_active": c.is_active,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in coupons
    ]
    return items


@router.post("/coupons")
async def create_coupon(
    req: CouponCreateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new discount coupon."""
    code_clean = req.code.strip().upper()
    if not code_clean:
        raise HTTPException(status_code=400, detail="کد تخفیف نمی‌تواند خالی باشد.")

    existing = (
        await db.execute(select(Coupon).where(func.upper(Coupon.code) == code_clean))
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="کد تخفیف از قبل وجود دارد.")

    valid_until = (
        datetime.now(timezone.utc) + timedelta(days=req.valid_days)
        if req.valid_days > 0
        else None
    )

    coupon = Coupon(
        id=uuid.uuid4(),
        code=code_clean,
        discount_percent=req.discount_percent,
        discount_amount_irr=req.discount_amount_toman * 10,
        max_uses=req.max_uses,
        current_uses=0,
        valid_until=valid_until,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(coupon)
    await record_audit(
        db,
        action="COUPON_CREATED",
        details=f"Created coupon {coupon.code}: {req.discount_percent}% / {req.discount_amount_toman} Toman, max: {req.max_uses}",
        actor_id=admin.telegram_id,
        target_type="Coupon",
        target_id=str(coupon.id),
    )
    await db.commit()

    return {
        "success": True,
        "message": "کد تخفیف با موفقیت ایجاد شد.",
        "coupon": {
            "id": str(coupon.id),
            "code": coupon.code,
            "discount_percent": coupon.discount_percent,
            "discount_amount_toman": req.discount_amount_toman,
            "max_uses": coupon.max_uses,
            "current_uses": coupon.current_uses,
            "valid_until": coupon.valid_until.isoformat() if coupon.valid_until else None,
            "is_active": coupon.is_active,
            "created_at": coupon.created_at.isoformat() if coupon.created_at else None,
        },
    }


@router.delete("/coupons/{coupon_id}")
async def delete_coupon(
    coupon_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a coupon code by id."""
    coupon = await db.get(Coupon, coupon_id)
    if not coupon:
        raise HTTPException(status_code=404, detail="کد تخفیف یافت نشد.")

    code = coupon.code
    await db.delete(coupon)
    await record_audit(
        db,
        action="COUPON_DELETED",
        details=f"Deleted coupon {code}",
        actor_id=admin.telegram_id,
        target_type="Coupon",
        target_id=str(coupon_id),
    )
    await db.commit()

    return {"success": True, "message": f"کد تخفیف {code} با موفقیت حذف شد."}


# -------------------------------------------------------------
# 14. Audit Logs API
# -------------------------------------------------------------
@router.get("/audit-logs")
async def list_audit_logs(
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    search: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve comprehensive audit logs and user activity history with filters."""
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))

    if action and action != "ALL":
        if action == "NEW_USERS":
            query = query.where(AuditLog.action == "NEW_USER_REGISTERED")
            count_query = count_query.where(AuditLog.action == "NEW_USER_REGISTERED")
        elif action == "USER_STARTS":
            query = query.where(AuditLog.action.in_(["NEW_USER_REGISTERED", "USER_STARTED_BOT"]))
            count_query = count_query.where(AuditLog.action.in_(["NEW_USER_REGISTERED", "USER_STARTED_BOT"]))
        elif action == "VOUCHERS":
            query = query.where(AuditLog.action == "VOUCHER_REDEEMED")
            count_query = count_query.where(AuditLog.action == "VOUCHER_REDEEMED")
        elif action == "ORDERS":
            query = query.where(AuditLog.action.in_(["ORDER_CREATED", "ORDER_APPROVED", "PAYMENT_COMPLETED"]))
            count_query = count_query.where(AuditLog.action.in_(["ORDER_CREATED", "ORDER_APPROVED", "PAYMENT_COMPLETED"]))
        elif action == "RENEWALS":
            query = query.where(AuditLog.action == "SUBSCRIPTION_RENEWED")
            count_query = count_query.where(AuditLog.action == "SUBSCRIPTION_RENEWED")
        elif action == "WALLET":
            query = query.where(AuditLog.action.in_(["WALLET_TOPUP", "WALLET_DEBIT"]))
            count_query = count_query.where(AuditLog.action.in_(["WALLET_TOPUP", "WALLET_DEBIT"]))
        elif action == "TRIALS":
            query = query.where(AuditLog.action == "TRIAL_ACTIVATED")
            count_query = count_query.where(AuditLog.action == "TRIAL_ACTIVATED")
        elif action == "ADMIN_ACTIONS":
            query = query.where(AuditLog.actor_role.in_(["SUPER_ADMIN", "ADMIN", "SYSTEM"]))
            count_query = count_query.where(AuditLog.actor_role.in_(["SUPER_ADMIN", "ADMIN", "SYSTEM"]))
        else:
            query = query.where(AuditLog.action == action)
            count_query = count_query.where(AuditLog.action == action)

    if search and search.strip():
        term = f"%{search.strip()}%"
        s_filter = (
            AuditLog.details.ilike(term)
            | AuditLog.action.ilike(term)
            | AuditLog.target_id.ilike(term)
        )
        if search.strip().isdigit():
            s_filter = s_filter | (AuditLog.actor_id == int(search.strip()))
        query = query.where(s_filter)
        count_query = count_query.where(s_filter)

    total = (await db.execute(count_query)).scalar() or 0
    logs = (
        await db.execute(
            query.order_by(AuditLog.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    # Preload user info for actors
    actor_ids = {log.actor_id for log in logs if log.actor_id}
    user_map = {}
    if actor_ids:
        users = (await db.execute(select(User).where(User.telegram_id.in_(actor_ids)))).scalars().all()
        for u in users:
            name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "کاربر"
            user_map[u.telegram_id] = {
                "name": name,
                "username": u.username,
                "role": u.role,
            }

    items = []
    for log in logs:
        u_info = user_map.get(log.actor_id) or {}
        items.append({
            "id": str(log.id),
            "action": log.action,
            "details": log.details,
            "actor_id": log.actor_id,
            "actor_role": log.actor_role,
            "actor_name": u_info.get("name"),
            "actor_username": u_info.get("username"),
            "target_type": log.target_type,
            "target_id": log.target_id,
            "ip_address": log.ip_address,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        })

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_new_users = (await db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.action == "NEW_USER_REGISTERED", AuditLog.created_at >= today_start)
    )).scalar() or 0
    today_vouchers = (await db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.action == "VOUCHER_REDEEMED", AuditLog.created_at >= today_start)
    )).scalar() or 0
    today_orders = (await db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.action.in_(["ORDER_APPROVED", "PAYMENT_COMPLETED"]), AuditLog.created_at >= today_start)
    )).scalar() or 0

    return {
        "total": total,
        "items": items,
        "stats": {
            "today_new_users": today_new_users,
            "today_vouchers": today_vouchers,
            "today_orders": today_orders,
            "total_logs": total,
        }
    }


# -------------------------------------------------------------
# 15. Physical Scratch Voucher Cards API & Legacy Import
# -------------------------------------------------------------
@router.post("/vouchers/generate")
async def generate_vouchers_endpoint(
    req: VoucherGenerateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Generate a batch of physical scratch voucher codes."""
    voucher_service = VoucherService(db)
    try:
        v_type = "wallet_credit" if req.voucher_type in ("wallet", "wallet_credit") else "plan"
        # 0 explicitly means UNLIMITED (no expiry date)
        if req.duration_days == 0 or req.expires_in_days == 0:
            d_days = 0
        elif req.duration_days is not None:
            d_days = req.duration_days
        elif req.expires_in_days is not None:
            d_days = req.expires_in_days
        else:
            d_days = 180

        batch_id, vouchers = await voucher_service.generate_batch(
            count=req.count,
            voucher_type=v_type,
            amount_toman=req.amount_toman or 0,
            product_id=req.product_id if v_type == "plan" else None,
            duration_days=d_days,
        )
        await record_audit(
            db,
            action="VOUCHERS_GENERATED",
            details=f"Generated {len(vouchers)} vouchers (Batch: {batch_id}, Type: {v_type})",
            actor_id=admin.telegram_id,
            target_type="Voucher",
            target_id=batch_id,
        )
        await db.commit()

        return {
            "success": True,
            "batch_id": batch_id,
            "total": len(vouchers),
            "count": len(vouchers),
            "codes": [v.code for v in vouchers],
        }
    except VPNPrivateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating vouchers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"خطا در تولید ووچرها: {e}")


@router.get("/vouchers/template")
async def download_voucher_template(
    admin: User = Depends(get_current_admin),
):
    """Download sample Excel (.xlsx) template for voucher import with 3 required columns."""
    excel_bytes = generate_voucher_template_excel()
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="voucher_import_template.xlsx"'},
    )


@router.post("/vouchers/upload")
async def upload_vouchers_file(
    file: UploadFile = File(...),
    product_id: Optional[str] = Form(None),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload Excel (.xlsx) or CSV file containing 3 required columns:
    Col 1: duration in months (12 months = 365 days fix)
    Col 2: username
    Col 3: password
    """
    filename = file.filename or "vouchers.xlsx"
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="فایل آپلود شده خالی است.")

    try:
        parsed_vouchers = parse_vouchers_from_bytes(contents, filename)
    except Exception as e:
        logger.error(f"Failed to parse voucher file: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"خطا در خواندن فایل اکسل/CSV: {e}")

    if not parsed_vouchers:
        raise HTTPException(status_code=400, detail="هیچ داده معتبری در فایل یافت نشد. لطفاً از فایل نمونه الگوبرداری فرمایید.")

    voucher_service = VoucherService(db)
    imported, skipped, batch_id = await voucher_service.import_parsed_vouchers(
        vouchers_data=parsed_vouchers,
        product_id=product_id,
        batch_prefix="XLS",
    )

    await record_audit(
        db,
        action="VOUCHERS_EXCEL_UPLOAD",
        details=f"Uploaded {imported} vouchers from {filename} (Batch: {batch_id}, Skipped: {skipped})",
        actor_id=admin.telegram_id,
        target_type="Voucher",
        target_id=batch_id,
    )
    await db.commit()

    return {
        "success": True,
        "batch_id": batch_id,
        "imported_count": imported,
        "skipped_count": skipped,
        "total_parsed": len(parsed_vouchers),
        "message": f"تعداد {imported} ووچر با موفقیت ذخیره شد ({skipped} مورد تکراری یا نامعتبر نادیده گرفته شد).",
    }


@router.get("/vouchers/export-unused")
async def export_unused_vouchers_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Download an Excel (.xlsx) file containing all unused vouchers with 3 required parameters."""
    voucher_service = VoucherService(db)
    unused_list = await voucher_service.get_unused_vouchers_for_export()
    excel_bytes = export_unused_vouchers_excel(unused_list)

    now_str = datetime.now().strftime("%Y%m%d_%H%M")
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="unused_vouchers_{now_str}.xlsx"'},
    )


@router.post("/vouchers/manual-add")
async def manual_add_vouchers_endpoint(
    req: VoucherManualAddRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add vouchers manually (single or bulk text lines)."""
    import re
    voucher_service = VoucherService(db)
    items_to_import = []

    if req.mode == "single":
        if not req.username and not req.password:
            raise HTTPException(status_code=400, detail="وارد کردن نام کاربری یا رمز عبور الزامی است.")
        m = req.duration_months or 1
        items_to_import.append({
            "duration_months": m,
            "duration_days": calculate_duration_days(m),
            "username": (req.username or "").strip(),
            "password": (req.password or "").strip(),
        })
    else:
        # Bulk text data (comma, tab, or space separated lines)
        raw_text = (req.text_data or "").strip()
        if not raw_text:
            raise HTTPException(status_code=400, detail="متن ورودی کدهای گروهی خالی است.")
        for line in raw_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in re.split(r'[,;\t]+', line) if p.strip()]
            if len(parts) < 2:
                parts = [p.strip() for p in line.split() if p.strip()]
            if not parts:
                continue

            try:
                str_m = parts[0]
                for fa, en in zip("۰۱۲۳۴۵۶۷۸۹", "0123456789"):
                    str_m = str_m.replace(fa, en)
                m = int(float(str_m))
            except Exception:
                m = 1

            u = parts[1] if len(parts) > 1 else ""
            p = parts[2] if len(parts) > 2 else ""
            if u or p:
                items_to_import.append({
                    "duration_months": m,
                    "duration_days": calculate_duration_days(m),
                    "username": u,
                    "password": p,
                })

    if not items_to_import:
        raise HTTPException(status_code=400, detail="هیچ داده معتبری جهت ثبت یافت نشد.")

    imported, skipped, batch_id = await voucher_service.import_parsed_vouchers(
        vouchers_data=items_to_import,
        product_id=req.product_id,
        batch_prefix="MAN",
    )

    await record_audit(
        db,
        action="VOUCHERS_MANUAL_ADD",
        details=f"Manually added {imported} vouchers (Batch: {batch_id}, Skipped: {skipped})",
        actor_id=admin.telegram_id,
        target_type="Voucher",
        target_id=batch_id,
    )
    await db.commit()

    return {
        "success": True,
        "batch_id": batch_id,
        "imported_count": imported,
        "skipped_count": skipped,
        "total_submitted": len(items_to_import),
        "message": f"تعداد {imported} ووچر با موفقیت ثبت شد ({skipped} مورد تکراری نادیده گرفته شد).",
    }


@router.delete("/vouchers/used/purge")
async def purge_used_vouchers_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Purge and delete all used vouchers (is_used == True) from the system."""
    voucher_service = VoucherService(db)
    deleted_count = await voucher_service.delete_used_vouchers()

    await record_audit(
        db,
        action="PURGE_USED_VOUCHERS",
        details=f"Purged {deleted_count} used vouchers",
        actor_id=admin.telegram_id,
        target_type="Voucher",
    )
    await db.commit()

    return {
        "success": True,
        "deleted_count": deleted_count,
        "message": f"تعداد {deleted_count} کارت ووچر مصرف‌شده با موفقیت از سیستم پاک‌سازی شدند.",
    }


@router.put("/vouchers/{voucher_id}")
async def update_voucher_endpoint(
    voucher_id: uuid.UUID,
    req: VoucherUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update single voucher details."""
    v = await db.get(Voucher, voucher_id)
    if not v:
        raise HTTPException(status_code=404, detail="ووچر مورد نظر یافت نشد.")

    if req.username is not None:
        v.username = req.username.strip() or None
    if req.password is not None:
        v.password = req.password.strip() or None
    if req.duration_months is not None:
        v.duration_months = max(1, req.duration_months)
        v.duration_days = calculate_duration_days(v.duration_months)
    if req.product_id is not None:
        if str(req.product_id).lower() in ("auto", "none", ""):
            v.product_id = None
        else:
            try:
                v.product_id = uuid.UUID(str(req.product_id))
            except Exception:
                pass
    if req.is_used is not None:
        v.is_used = req.is_used
        if not req.is_used:
            v.used_at = None
            v.used_by_telegram_id = None

    await db.commit()
    await db.refresh(v)
    return {"success": True, "message": "اطلاعات ووچر با موفقیت ویرایش شد."}


@router.post("/vouchers/import")
async def import_vouchers_endpoint(
    req: VoucherImportRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Import existing custom or historical voucher codes into the system."""
    if not req.codes:
        raise HTTPException(status_code=400, detail="حداقل یک کد ووچر برای ثبت الزامی است.")

    v_type = "wallet_credit" if req.voucher_type in ("wallet", "wallet_credit") else "plan"
    prod_id = None
    if v_type == "plan" and req.product_id:
        try:
            prod_id = uuid.UUID(str(req.product_id))
        except Exception:
            prod_id = req.product_id

    batch_id = f"imp_{uuid.uuid4().hex[:8]}"
    expires_at = None
    if req.duration_days and req.duration_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=req.duration_days)

    cleaned_codes = []
    seen = set()
    for raw in req.codes:
        c = raw.strip().upper()
        if c and c not in seen:
            seen.add(c)
            cleaned_codes.append(c)

    if not cleaned_codes:
        raise HTTPException(status_code=400, detail="هیچ کد معتبری یافت نشد.")

    # Check duplicates in DB
    existing_q = await db.execute(select(Voucher.code).where(Voucher.code.in_(cleaned_codes)))
    existing_set = set(existing_q.scalars().all())

    new_vouchers = []
    skipped_count = 0
    for code in cleaned_codes:
        if code in existing_set:
            skipped_count += 1
            continue
        v = Voucher(
            code=code,
            voucher_type=v_type,
            amount_toman=req.amount_toman if v_type == "wallet_credit" else 0,
            product_id=prod_id if v_type == "plan" else None,
            batch_id=batch_id,
            expires_at=expires_at,
            is_used=False,
        )
        db.add(v)
        new_vouchers.append(v)

    if new_vouchers:
        await record_audit(
            db,
            action="VOUCHERS_IMPORTED",
            details=f"Imported {len(new_vouchers)} vouchers (Batch: {batch_id}, Skipped duplicate: {skipped_count})",
            actor_id=admin.telegram_id,
            target_type="Voucher",
            target_id=batch_id,
        )
        await db.commit()

    return {
        "success": True,
        "batch_id": batch_id,
        "imported_count": len(new_vouchers),
        "skipped_count": skipped_count,
        "total_submitted": len(cleaned_codes),
    }


@router.get("/vouchers")
async def list_vouchers_endpoint(
    batch_id: Optional[str] = None,
    is_used: Optional[bool] = None,
    voucher_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List vouchers with pagination and filters."""
    query = select(Voucher).options(selectinload(Voucher.product))
    count_query = select(func.count(Voucher.id))

    filters = []
    if batch_id:
        filters.append(Voucher.batch_id == batch_id)
    if is_used is not None:
        filters.append(Voucher.is_used == is_used)
    if voucher_type:
        filters.append(Voucher.voucher_type == voucher_type)
    if search:
        filters.append(or_(
            Voucher.code.ilike(f"%{search.strip()}%"),
            Voucher.username.ilike(f"%{search.strip()}%"),
        ))

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total = (await db.execute(count_query)).scalar() or 0
    vouchers = (
        await db.execute(
            query.order_by(Voucher.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    items = [
        {
            "id": str(v.id),
            "code": v.code,
            "username": v.username,
            "password": v.password,
            "duration_months": v.duration_months or 1,
            "duration_days": v.duration_days or 30,
            "voucher_type": v.voucher_type,
            "amount_toman": v.amount_toman,
            "product_id": str(v.product_id) if v.product_id else None,
            "product_name": v.product.name if v.product else None,
            "is_used": v.is_used,
            "used_by_telegram_id": v.used_by_telegram_id,
            "used_at": v.used_at.isoformat() if v.used_at else None,
            "batch_id": v.batch_id,
            "expires_at": v.expires_at.isoformat() if v.expires_at else None,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in vouchers
    ]

    return {"total": total, "items": items}


@router.delete("/vouchers/{voucher_id}")
async def delete_voucher_endpoint(
    voucher_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single voucher card."""
    voucher = await db.get(Voucher, voucher_id)
    if not voucher:
        raise HTTPException(status_code=404, detail="ووچر مورد نظر یافت نشد.")

    code = voucher.code
    await db.delete(voucher)
    await record_audit(
        db,
        action="VOUCHER_DELETED",
        details=f"Deleted voucher {code}",
        actor_id=admin.telegram_id,
        target_type="Voucher",
        target_id=str(voucher_id),
    )
    await db.commit()
    return {"success": True, "message": f"ووچر {code} با موفقیت حذف شد."}


@router.delete("/vouchers/batch/{batch_id}")
async def delete_voucher_batch_endpoint(
    batch_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete all unused vouchers in a batch."""
    stmt = select(Voucher).where(Voucher.batch_id == batch_id, Voucher.is_used == False)
    vouchers = (await db.execute(stmt)).scalars().all()
    if not vouchers:
        raise HTTPException(status_code=404, detail="هیچ ووچر استفاده نشده‌ای در این دوره یافت نشد.")

    count = len(vouchers)
    for v in vouchers:
        await db.delete(v)

    await record_audit(
        db,
        action="VOUCHER_BATCH_DELETED",
        details=f"Deleted {count} unused vouchers in batch {batch_id}",
        actor_id=admin.telegram_id,
        target_type="Voucher",
        target_id=batch_id,
    )
    await db.commit()
    return {
        "success": True,
        "deleted_count": count,
        "message": f"{count} ووچر استفاده نشده از دوره با موفقیت حذف شدند.",
    }


@router.get("/vouchers/print/{batch_id}", response_class=Response)
async def print_vouchers_batch_endpoint(
    batch_id: str,
    bot_username: Optional[str] = Query(None),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Returns a clean, high-resolution printable HTML page with standard card layout."""

    query = (
        select(Voucher)
        .where(Voucher.batch_id == batch_id)
        .options(selectinload(Voucher.product))
        .order_by(Voucher.created_at.asc(), Voucher.code.asc())
    )
    vouchers = (await db.execute(query)).scalars().all()
    if not vouchers:
        raise HTTPException(status_code=404, detail="هیچ ووچری در این دوره یافت نشد.")

    from app.services.settings_service import get_effective_bot_username
    if not bot_username or not bot_username.strip():
        bot_username = await get_effective_bot_username(db)
    clean_bot = bot_username.lstrip("@").strip() if bot_username and bot_username.strip() else "VPNPrivateSell_Bot"

    html_content = render_printable_vouchers_html(
        vouchers=vouchers,
        batch_id=batch_id,
        bot_username=clean_bot,
    )
    return Response(content=html_content, media_type="text/html")


# =====================================================================
# 22. Dynamic Bot Texts & Referral Management Endpoints
# =====================================================================

@router.get("/bot/texts")
async def get_bot_texts_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve all dynamic Telegram bot texts with titles and descriptions along with channel lock and support settings."""
    from app.modules.bot_texts.service import get_all_bot_texts
    from app.services.settings_service import SettingsService, get_effective_bot_username
    items = get_all_bot_texts()
    bot_user = await get_effective_bot_username(db)
    raw_suffix = await SettingsService.get_value("CLIENT_EMAIL_SUFFIX") or ""
    raw_prefix = await SettingsService.get_value("CONFIG_TITLE_PREFIX") or "VPN-Private"
    eff_suffix = await SettingsService.get_effective_email_suffix(db)
    eff_prefix = await SettingsService.get_effective_config_title_prefix(db)

    return {
        "items": items,
        "total": len(items),
        "texts": {item["key"]: item["current_value"] for item in items},
        "channel_lock": {
            "enabled": settings.MANDATORY_CHANNEL_ENABLED,
            "channel_username": settings.CHANNEL_USERNAME,
        },
        "support": {
            "support_username": settings.SUPPORT_USERNAME,
        },
        "bot_username": bot_user,
        "bot": {
            "bot_username": bot_user,
        },
        "client_email_suffix": str(raw_suffix) if raw_suffix else "",
        "config_title_prefix": str(raw_prefix) if raw_prefix else "VPN-Private",
        "effective_email_suffix": eff_suffix,
        "effective_config_title_prefix": eff_prefix,
    }


@router.put("/bot/texts")
async def update_bot_texts_endpoint(
    payload: Dict[str, Any],
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update dynamic Telegram bot texts, channel lock, support username, email suffix, and config prefix."""
    from app.modules.bot_texts.service import update_bot_texts, get_all_bot_texts
    from app.services.settings_service import SettingsService
    clean_texts = {}
    raw_dict = payload.get("texts") if isinstance(payload.get("texts"), dict) else payload

    # 1. Update channel lock settings if provided
    if "channel_lock_enabled" in payload:
        settings.MANDATORY_CHANNEL_ENABLED = bool(payload["channel_lock_enabled"])
    elif isinstance(raw_dict, dict) and "channel_lock_enabled" in raw_dict:
        settings.MANDATORY_CHANNEL_ENABLED = bool(raw_dict["channel_lock_enabled"])

    if "channel_username" in payload:
        ch = str(payload["channel_username"]).strip().lstrip("@")
        if ch:
            settings.CHANNEL_USERNAME = ch
    elif isinstance(raw_dict, dict) and "channel_username" in raw_dict:
        ch = str(raw_dict["channel_username"]).strip().lstrip("@")
        if ch:
            settings.CHANNEL_USERNAME = ch

    # 2. Update support username if provided
    if "support_username" in payload:
        sup = str(payload["support_username"]).strip().lstrip("@")
        if sup:
            settings.SUPPORT_USERNAME = sup
    elif isinstance(raw_dict, dict) and "support_username" in raw_dict:
        sup = str(raw_dict["support_username"]).strip().lstrip("@")
        if sup:
            settings.SUPPORT_USERNAME = sup

    # 3. Update bot username if provided
    bot_user_val = None
    if "bot_username" in payload:
        bot_user_val = str(payload["bot_username"]).strip().lstrip("@")
    elif isinstance(raw_dict, dict) and "bot_username" in raw_dict:
        bot_user_val = str(raw_dict["bot_username"]).strip().lstrip("@")

    if bot_user_val:
        settings.BOT_USERNAME = bot_user_val
        update_env_variable("BOT_USERNAME", bot_user_val)
        await SettingsService.set_value("BOT_USERNAME", bot_user_val, session=db)

    # 4. Update client_email_suffix if provided
    if "client_email_suffix" in payload or (isinstance(raw_dict, dict) and "client_email_suffix" in raw_dict):
        raw_sfx = payload.get("client_email_suffix") if "client_email_suffix" in payload else raw_dict.get("client_email_suffix")
        clean_sfx = str(raw_sfx).strip() if raw_sfx is not None else ""
        if clean_sfx and not clean_sfx.startswith("@"):
            clean_sfx = f"@{clean_sfx}"
        await SettingsService.set_value("CLIENT_EMAIL_SUFFIX", clean_sfx, session=db)

    # 5. Update config_title_prefix if provided
    if "config_title_prefix" in payload or (isinstance(raw_dict, dict) and "config_title_prefix" in raw_dict):
        raw_pfx = payload.get("config_title_prefix") if "config_title_prefix" in payload else raw_dict.get("config_title_prefix")
        clean_pfx = str(raw_pfx).strip() if raw_pfx is not None else "VPN-Private"
        await SettingsService.set_value("CONFIG_TITLE_PREFIX", clean_pfx, session=db)

    if isinstance(payload, dict):
        for k, v in raw_dict.items():
            if k in (
                "channel_lock_enabled",
                "channel_username",
                "support_username",
                "bot_username",
                "client_email_suffix",
                "config_title_prefix",
                "effective_email_suffix",
                "effective_config_title_prefix",
            ):
                continue
            if isinstance(v, str):
                clean_texts[k] = v
            elif isinstance(v, dict) and "current_value" in v:
                clean_texts[k] = str(v["current_value"])
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and "key" in item and "current_value" in item:
                clean_texts[str(item["key"])] = str(item["current_value"])

    if clean_texts:
        update_bot_texts(clean_texts)
    await record_audit(
        db,
        action="BOT_TEXTS_UPDATED",
        details=f"Updated {len(clean_texts)} bot texts: {list(clean_texts.keys())}, channel_lock={settings.MANDATORY_CHANNEL_ENABLED}, channel=@{settings.CHANNEL_USERNAME}, support=@{settings.SUPPORT_USERNAME}, bot=@{settings.BOT_USERNAME}",
        actor_id=admin.telegram_id,
    )
    await db.commit()
    return {
        "success": True,
        "message": "تنظیمات کانال، پشتیبانی و متن‌های ربات با موفقیت ذخیره شدند.",
        "items": get_all_bot_texts(),
        "channel_lock": {
            "enabled": settings.MANDATORY_CHANNEL_ENABLED,
            "channel_username": settings.CHANNEL_USERNAME,
        },
        "support": {
            "support_username": settings.SUPPORT_USERNAME,
        },
        "bot_username": settings.BOT_USERNAME,
        "bot": {
            "bot_username": settings.BOT_USERNAME,
        },
        "client_email_suffix": str(await SettingsService.get_value("CLIENT_EMAIL_SUFFIX") or ""),
        "config_title_prefix": str(await SettingsService.get_value("CONFIG_TITLE_PREFIX") or "VPN-Private"),
        "effective_email_suffix": await SettingsService.get_effective_email_suffix(db),
        "effective_config_title_prefix": await SettingsService.get_effective_config_title_prefix(db),
    }


@router.get("/referrals")
async def get_referrals_report(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get full referral statistics, current commission percent, and list of all referrers with invited users."""
    total_commission_res = await db.execute(
        select(func.coalesce(func.sum(Referral.total_commission_irr), 0))
    )
    total_commission_irr = total_commission_res.scalar() or 0

    total_referred_res = await db.execute(
        select(func.count(User.id)).where(User.referred_by_id.is_not(None))
    )
    total_referred_users = total_referred_res.scalar() or 0

    referrer_ids_res = await db.execute(
        select(User.referred_by_id).where(User.referred_by_id.is_not(None)).distinct()
    )
    referrer_ids = [r for r in referrer_ids_res.scalars().all() if r is not None]

    referrers_data = []
    for ref_id in referrer_ids:
        referrer = await db.get(User, ref_id)
        if not referrer:
            continue

        referred_users_res = await db.execute(
            select(User).where(User.referred_by_id == ref_id).order_by(User.created_at.desc())
        )
        referred_users = list(referred_users_res.scalars().all())

        records_res = await db.execute(
            select(Referral).where(Referral.referrer_user_id == ref_id)
        )
        records_by_referred = {r.referred_user_id: r for r in records_res.scalars().all()}

        referrer_commission_irr = sum(r.total_commission_irr for r in records_by_referred.values())

        users_list = []
        for u in referred_users:
            rec = records_by_referred.get(u.id)
            status_str = rec.status if rec else "PENDING"
            u_comm_irr = rec.total_commission_irr if rec else 0
            u_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "کاربر تلگرام"
            users_list.append({
                "id": str(u.id),
                "telegram_id": u.telegram_id,
                "username": u.username,
                "name": u_name,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "status": status_str,
                "commission_irr": u_comm_irr,
                "commission_toman": u_comm_irr // 10,
            })

        ref_name = f"{referrer.first_name or ''} {referrer.last_name or ''}".strip() or "کاربر تلگرام"
        referrers_data.append({
            "user_id": str(referrer.id),
            "telegram_id": referrer.telegram_id,
            "username": referrer.username,
            "name": ref_name,
            "referral_code": referrer.referral_code,
            "invited_count": len(referred_users),
            "total_commission_irr": referrer_commission_irr,
            "total_commission_toman": referrer_commission_irr // 10,
            "referred_users": users_list,
        })

    referrers_data.sort(key=lambda x: (x["invited_count"], x["total_commission_irr"]), reverse=True)

    return {
        "commission_percent": settings.REFERRAL_COMMISSION_PERCENT,
        "total_referrers": len(referrers_data),
        "total_referred_users": total_referred_users,
        "total_commission_irr": total_commission_irr,
        "total_commission_toman": total_commission_irr // 10,
        "referrers": referrers_data,
        "items": referrers_data,
    }


@router.post("/referrals/settings")
async def update_referral_settings_endpoint(
    req: ReferralSettingsUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update referral commission percent and sync to .env file."""
    settings.REFERRAL_COMMISSION_PERCENT = req.commission_percent
    await record_audit(
        db,
        action="REFERRAL_COMMISSION_UPDATED",
        details=f"Updated referral commission percent to {req.commission_percent}%",
        actor_id=admin.telegram_id,
    )
    await db.commit()
    return {
        "success": True,
        "commission_percent": settings.REFERRAL_COMMISSION_PERCENT,
        "message": f"درصد پورسانت معرف به {req.commission_percent}٪ تغییر یافت.",
    }


# =========================================================================
# RESELLER & WHOLESALE MANAGEMENT ENDPOINTS
# =========================================================================

class AdminCreateResellerRequest(BaseModel):
    user_identifier: str = Field(..., description="Telegram ID, Username, or User UUID")
    discount_percent: int = Field(default=20, ge=0, le=100)
    credit_limit_toman: int = Field(default=0, ge=0)
    is_active: bool = True


class AdminUpdateResellerRequest(BaseModel):
    discount_percent: Optional[int] = Field(None, ge=0, le=100)
    credit_limit_toman: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None


@router.get("/resellers")
async def list_resellers_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve all wholesale partners and resellers with their individual discount rates."""
    resellers = await ResellerService.get_all_resellers(db)
    return {
        "success": True,
        "total": len(resellers),
        "items": resellers,
    }


@router.post("/resellers")
async def create_reseller_endpoint(
    req: AdminCreateResellerRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Add new reseller or upgrade existing user with custom discount percent."""
    try:
        res = await ResellerService.add_or_update_reseller(
            db=db,
            user_identifier=req.user_identifier,
            discount_percent=req.discount_percent,
            credit_limit_toman=req.credit_limit_toman,
            is_active=req.is_active,
        )
        await record_audit(
            db,
            action="RESELLER_CREATED_OR_UPDATED",
            details=f"User {req.user_identifier} set as Reseller with {req.discount_percent}% discount",
            actor_id=admin.telegram_id,
        )
        return {
            "success": True,
            "message": "نماینده با موفقیت ثبت/بروزرسانی شد.",
            "reseller": res,
        }
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        logger.error(f"Error creating reseller: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="خطا در ثبت نماینده جدید.")


@router.put("/resellers/{reseller_id}")
async def update_reseller_endpoint(
    reseller_id: uuid.UUID,
    req: AdminUpdateResellerRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update individual reseller discount percentage, credit limit, or active state."""
    try:
        updated = await ResellerService.update_reseller(
            db=db,
            reseller_id=reseller_id,
            discount_percent=req.discount_percent,
            credit_limit_toman=req.credit_limit_toman,
            is_active=req.is_active,
        )
        await record_audit(
            db,
            action="RESELLER_UPDATED",
            details=f"Reseller {reseller_id} updated: discount={req.discount_percent}%, credit={req.credit_limit_toman}",
            actor_id=admin.telegram_id,
        )
        return {
            "success": True,
            "message": "تنظیمات نماینده با موفقیت ذخیره شد.",
            "reseller": updated,
        }
    except ValueError as val_err:
        raise HTTPException(status_code=404, detail=str(val_err))
    except Exception as exc:
        logger.error(f"Error updating reseller: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="خطا در ویرایش اطلاعات نماینده.")


@router.delete("/resellers/{reseller_id}")
async def delete_reseller_endpoint(
    reseller_id: uuid.UUID,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Demote reseller back to customer and remove reseller privileges."""
    success = await ResellerService.delete_reseller(db=db, reseller_id=reseller_id)
    if not success:
        raise HTTPException(status_code=404, detail="نماینده یافت نشد.")
    await record_audit(
        db,
        action="RESELLER_DELETED",
        details=f"Reseller {reseller_id} removed by admin",
        actor_id=admin.telegram_id,
    )
    return {
        "success": True,
        "message": "دسترسی نمایندگی کاربر لغو و به مشتری عادی تبدیل شد.",
    }


# =========================================================================
# LUCKY WHEEL ADMIN CONFIGURATION ENDPOINTS
# =========================================================================

@router.get("/lucky-wheel")
async def get_lucky_wheel_config_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Fetch current dynamic configuration and prizes for Daily Lucky Wheel."""
    config = await LuckyWheelService.get_config(db)
    return {
        "success": True,
        "config": config,
    }


@router.post("/lucky-wheel")
@router.put("/lucky-wheel")
async def update_lucky_wheel_config_endpoint(
    config_data: Dict[str, Any],
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Save updated dynamic options, prize labels, probabilities, and rewards for Lucky Wheel."""
    try:
        saved = await LuckyWheelService.save_config(config_data, db)
        await record_audit(
            db,
            action="LUCKY_WHEEL_CONFIG_UPDATED",
            details="Admin updated Lucky Wheel options and reward probabilities",
            actor_id=admin.telegram_id,
        )
        return {
            "success": True,
            "message": "تنظیمات گردونه شانس با موفقیت ذخیره شد.",
            "config": saved,
        }
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as exc:
        logger.error(f"Error saving lucky wheel config: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="خطا در ذخیره تنظیمات گردونه شانس.")


# =========================================================================
# FLASH SALE & WIN-BACK MARKETING ADMIN ENDPOINTS
# =========================================================================

class FlashSaleAdminRequest(BaseModel):
    is_enabled: bool = True
    discount_percent: int = 30
    title: str = "⚡️ تخفیف ویژه صاعقه‌ای پایان هفته"
    duration_hours: Optional[float] = None
    ends_at: Optional[str] = None


@router.get("/flash-sale")
async def get_admin_flash_sale(
    admin: User = Depends(get_current_admin),
):
    """Get current flash sale settings for admin management."""
    from app.services.flash_sale_service import FlashSaleService
    info = FlashSaleService.get_flash_sale_info()
    return {"success": True, "flash_sale": info}


@router.post("/flash-sale")
async def update_admin_flash_sale(
    req: FlashSaleAdminRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Enable, disable, or configure timed flash sale."""
    from app.services.flash_sale_service import FlashSaleService
    updated = await FlashSaleService.configure_flash_sale(
        is_enabled=req.is_enabled,
        discount_percent=req.discount_percent,
        title=req.title,
        duration_hours=req.duration_hours,
        ends_at_iso=req.ends_at,
        db_session=db,
    )
    await record_audit(
        db,
        action="FLASH_SALE_CONFIGURED",
        details=f"Admin set flash sale: enabled={req.is_enabled}, discount={req.discount_percent}%, duration={req.duration_hours}h",
        actor_id=admin.telegram_id,
    )
    return {
        "success": True,
        "message": "تنظیمات تخفیف صاعقه‌ای با موفقیت به‌روزرسانی شد.",
        "flash_sale": updated,
    }


class WinbackSettingsUpdateRequest(BaseModel):
    is_enabled: bool = Field(True, description="Enable or disable automated winback campaign")
    discount_percent: int = Field(35, ge=1, le=100, description="Discount percentage for winback offer")
    coupon_code: str = Field("COMEBACK35", min_length=2, max_length=32, description="Coupon code for winback offer")


@router.get("/winback")
async def get_admin_winback_settings(
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """Get current Win-Back campaign configuration."""
    from app.services.winback_service import WinbackService
    return {
        "success": True,
        "winback": WinbackService.get_winback_settings(),
    }


@router.post("/winback")
async def update_admin_winback_settings(
    req: WinbackSettingsUpdateRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Update Win-Back campaign configuration and coupon discount."""
    from app.services.winback_service import WinbackService
    updated = await WinbackService.update_winback_settings(
        is_enabled=req.is_enabled,
        discount_percent=req.discount_percent,
        coupon_code=req.coupon_code,
        session=db,
    )
    await record_audit(
        db,
        action="WINBACK_SETTINGS_UPDATED",
        details=f"Admin updated winback settings: enabled={req.is_enabled}, discount={req.discount_percent}%, code={req.coupon_code}",
        actor_id=admin.telegram_id,
    )
    return {
        "success": True,
        "message": "تنظیمات کمپین بازگرداندن مشتریان با موفقیت ذخیره شد.",
        "winback": updated,
    }


@router.post("/winback/run-now")
async def run_winback_now_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger the Win-Back campaign scan for expired subscriptions."""
    from app.services.winback_service import WinbackService
    result = await WinbackService.process_winback_campaign(session=db, force=True)
    await record_audit(
        db,
        action="WINBACK_CAMPAIGN_TRIGGERED",
        details=f"Admin manually ran winback campaign: sent={result.get('sent', 0)}",
        actor_id=admin.telegram_id,
    )
    return {
        "success": True,
        "message": f"کمپین بازگرداندن مشتریان اجرا شد: {result.get('sent', 0)} پیام ارسال شد.",
        "result": result,
    }


class BulkCompensationRequest(BaseModel):
    days: int = Field(..., ge=1, le=60, description="Number of compensation days to add (1-60)")
    notify_users: bool = Field(True, description="Whether to send notification to users via Telegram")
    custom_message: Optional[str] = Field(None, max_length=4000, description="Optional custom message note")


@router.post("/compensation/apply")
async def apply_bulk_compensation_endpoint(
    req: BulkCompensationRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Add extra days to all active subscriptions for outage/downtime compensation."""
    from app.services.compensation_service import CompensationService
    try:
        res = await CompensationService.apply_bulk_compensation(
            days=req.days,
            notify_users=req.notify_users,
            custom_message=req.custom_message,
            session=db,
        )
        await record_audit(
            db,
            action="DOWNTIME_COMPENSATION_APPLIED",
            details=f"Admin applied {req.days} days compensation to {res.get('subscriptions_updated', 0)} subs (notify={req.notify_users})",
            actor_id=admin.telegram_id,
        )
        return res
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as e:
        logger.error(f"Error applying bulk compensation: {e}")
        raise HTTPException(status_code=500, detail="خطا در اعمال روزهای جبران قطعی.")


# ==============================================================================
# 28. Balancer Location Matrix Management (PIA & Windscribe)
# ==============================================================================

class ToggleBalancerLocationRequest(BaseModel):
    balancer_tag: str
    is_active: bool


class SetBalancerProviderRequest(BaseModel):
    balancer_tag: str
    active_provider: str


@router.get("/balancer-locations")
async def get_balancer_locations_endpoint(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Returns matrix of all balancer locations with PIA/Windscribe coverage and active state."""
    from app.services.balancer_location_service import BalancerLocationService
    locations = await BalancerLocationService.get_all_locations(session=db)
    summary = await BalancerLocationService.get_summary(session=db)
    return {
        "success": True,
        "summary": summary,
        "locations": locations,
    }


@router.post("/balancer-locations/toggle")
async def toggle_balancer_location_endpoint(
    req: ToggleBalancerLocationRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Toggles active/inactive state of a balancer location."""
    from app.services.balancer_location_service import BalancerLocationService
    res = await BalancerLocationService.toggle_location(
        balancer_tag=req.balancer_tag,
        is_active=req.is_active,
        session=db
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "خطا در تغییر وضعیت لوکیشن"))

    await record_audit(
        db,
        action="BALANCER_LOCATION_TOGGLED",
        details=f"Admin set balancer {req.balancer_tag} active={req.is_active}",
        actor_id=admin.telegram_id,
    )
    return res


@router.post("/balancer-locations/provider")
async def set_balancer_provider_endpoint(
    req: SetBalancerProviderRequest,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Sets active provider routing mode for a balancer ('both', 'pia', 'windscribe')."""
    from app.services.balancer_location_service import BalancerLocationService
    res = await BalancerLocationService.set_active_provider(
        balancer_tag=req.balancer_tag,
        active_provider=req.active_provider,
        session=db
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "خطا در تغییر ارائه‌دهنده"))
    return res









