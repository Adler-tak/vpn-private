"""Voucher Management & Redemption Service."""
import io
import base64
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any

import qrcode
from sqlalchemy import select, delete, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models.voucher import Voucher
from app.database.models.product import Product
from app.database.models.order import Order
from app.database.models.user import User
from app.core.constants import OrderStatus
from app.core.exceptions import VPNPrivateError
from app.modules.wallets.service import WalletService
from app.modules.subscriptions.service import SubscriptionService
from app.modules.vouchers.excel import calculate_duration_days

logger = logging.getLogger(__name__)

# Unambiguous character set (no O, 0, I, 1, L)
VOUCHER_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_voucher_code() -> str:
    """Generate secure random scratch voucher code format VPN-XXXX-XXXX-XXXX."""
    p1 = "".join(secrets.choice(VOUCHER_ALPHABET) for _ in range(4))
    p2 = "".join(secrets.choice(VOUCHER_ALPHABET) for _ in range(4))
    p3 = "".join(secrets.choice(VOUCHER_ALPHABET) for _ in range(4))
    return f"VPN-{p1}-{p2}-{p3}"


def normalize_voucher_code(raw_code: str) -> str:
    """Normalize user-entered voucher code: strip, uppercase, convert digits."""
    if not raw_code:
        return ""
    code = raw_code.strip()
    persian_digits = "۰۱۲۳۴۵۶۷۸۹"
    arabic_digits = "٠١٢٣٤٥٦٧٨٩"
    for i in range(10):
        code = code.replace(persian_digits[i], str(i)).replace(arabic_digits[i], str(i))
    code = code.upper()
    if code.startswith("V_") or code.startswith("V-"):
        code = code[2:]
    return code.strip()


def generate_qr_base64(content: str) -> str:
    """Generate high-contrast QR code as base64 PNG data URI."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=1,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#000000", back_color="#ffffff")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


class VoucherService:
    """Service handling voucher generation, batch operations, legacy import, and redemption."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def generate_batch(
        self,
        count: int,
        voucher_type: str = "plan",
        amount_toman: int = 0,
        product_id: Optional[Any] = None,
        duration_days: Optional[int] = None,
    ) -> Tuple[str, List[Voucher]]:
        """Generate a batch of unique voucher cards."""
        if count < 1 or count > 500:
            raise VPNPrivateError("تعداد ووچرها باید بین ۱ تا ۵۰۰ باشد.")

        if voucher_type not in ("plan", "wallet_credit"):
            raise VPNPrivateError("نوع ووچر نامعتبر است.")

        prod_uuid = None
        if voucher_type == "plan":
            if not product_id:
                raise VPNPrivateError("برای ووچر از نوع پلن، انتخاب محصول الزامی است.")
            prod = None
            try:
                prod_uuid = uuid.UUID(str(product_id)) if isinstance(product_id, str) else product_id
                prod = await self.session.get(Product, prod_uuid)
            except Exception:
                prod = None
            if not prod:
                prod = await self.session.get(Product, product_id)
            if not prod:
                raise VPNPrivateError("محصول انتخاب شده یافت نشد.")
            prod_uuid = prod.id
        elif voucher_type == "wallet_credit":
            if not amount_toman or amount_toman <= 0:
                raise VPNPrivateError("مبلغ اعتبار کیف پول باید بزرگتر از صفر باشد.")

        batch_id = f"BATCH-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3).upper()}"
        expires_at = None
        if duration_days and duration_days > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(days=duration_days)

        codes_set = set()
        while len(codes_set) < count:
            codes_set.add(generate_voucher_code())

        # Ensure no collision with existing codes in DB
        existing = set(
            (await self.session.execute(
                select(Voucher.code).where(Voucher.code.in_(codes_set))
            )).scalars().all()
        )
        while existing:
            codes_set -= existing
            while len(codes_set) < count:
                codes_set.add(generate_voucher_code())
            existing = set(
                (await self.session.execute(
                    select(Voucher.code).where(Voucher.code.in_(codes_set))
                )).scalars().all()
            )

        now = datetime.now(timezone.utc)
        vouchers = [
            Voucher(
                id=uuid.uuid4(),
                code=c,
                voucher_type=voucher_type,
                amount_toman=amount_toman if voucher_type == "wallet_credit" else 0,
                product_id=prod_uuid if voucher_type == "plan" else None,
                is_used=False,
                batch_id=batch_id,
                duration_days=duration_days if duration_days is not None else 30,
                expires_at=expires_at,
                created_at=now,
            )
            for c in codes_set
        ]

        self.session.add_all(vouchers)
        await self.session.flush()
        return batch_id, vouchers

    async def import_parsed_vouchers(
        self,
        vouchers_data: List[Dict[str, Any]],
        product_id: Optional[Any] = None,
        batch_prefix: str = "IMP",
    ) -> Tuple[int, int, str]:
        """
        Import parsed 3-column vouchers (months, username, password).
        Calculates exact duration_days (12 months = 365 days).
        Ensures each voucher is unique and single-use.
        Returns: (imported_count, skipped_count, batch_id)
        """
        if not vouchers_data:
            return 0, 0, ""

        batch_id = f"{batch_prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2).upper()}"
        now = datetime.now(timezone.utc)

        # Check existing codes and usernames in DB
        existing_codes_q = await self.session.execute(select(Voucher.code))
        existing_codes = set(existing_codes_q.scalars().all())

        existing_users_q = await self.session.execute(select(Voucher.username).where(Voucher.username.isnot(None)))
        existing_users = set(existing_users_q.scalars().all())

        prod_uuid = None
        if product_id and str(product_id).lower() not in ("auto", "none", ""):
            try:
                prod_uuid = uuid.UUID(str(product_id)) if isinstance(product_id, str) else product_id
                target_p = await self.session.get(Product, prod_uuid)
                if not target_p:
                    target_p = await self.session.get(Product, product_id)
                prod_uuid = target_p.id if target_p else None
            except Exception:
                prod_uuid = None

        imported = []
        skipped_count = 0
        seen_in_batch = set()

        for item in vouchers_data:
            months = int(item.get("duration_months") or 1)
            days = item.get("duration_days") or calculate_duration_days(months)
            u = (item.get("username") or "").strip()
            p = (item.get("password") or "").strip()

            if not u and not p:
                skipped_count += 1
                continue

            # Unique key for deduplication in current batch
            key = f"{u}:{p}".lower()
            if key in seen_in_batch or (u and u in existing_users):
                skipped_count += 1
                continue
            seen_in_batch.add(key)

            # Generate unique voucher code
            code = generate_voucher_code()
            while code in existing_codes:
                code = generate_voucher_code()
            existing_codes.add(code)

            v = Voucher(
                id=uuid.uuid4(),
                code=code,
                username=u or None,
                password=p or None,
                duration_months=months,
                duration_days=days,
                voucher_type="plan",
                amount_toman=0,
                product_id=prod_uuid,
                is_used=False,
                batch_id=batch_id,
                created_at=now,
            )
            imported.append(v)

        if imported:
            self.session.add_all(imported)
            await self.session.flush()

        return len(imported), skipped_count, batch_id

    async def redeem_voucher(
        self,
        raw_code: str,
        user: User,
    ) -> Dict[str, Any]:
        """
        Redeem a voucher card or legacy subscription for a user.
        Supports:
          1. Standard scratch voucher code (e.g. VPN-XXXX-XXXX-XXXX)
          2. Legacy username & password pair (e.g. 'username password', 'user:pass', 'user,pass')
          3. Legacy username alone
        Strict single-use: locks row and marks is_used=True.
        Provisions subscription for exact duration_days (365 days for 12 months).
        """
        raw_clean = (raw_code or "").strip()
        if not raw_clean:
            return {
                "success": False,
                "error": "EMPTY_CODE",
                "message": "❌ لطفاً کد ووچر یا مشخصات کاربری را وارد فرمایید.",
            }

        code_candidate = normalize_voucher_code(raw_clean)
        voucher: Optional[Voucher] = None

        # 1. Try finding by Voucher.code (e.g. VPN-XXXX-XXXX-XXXX)
        stmt_code = (
            select(Voucher)
            .where(Voucher.code == code_candidate)
            .options(selectinload(Voucher.product))
            .with_for_update()
        )
        voucher = (await self.session.execute(stmt_code)).scalar_one_or_none()

        # 2. If not found by code, check if input is "username password" or username:password
        if not voucher:
            parts = [p.strip() for p in re.split(r'[\s:,]+', raw_clean) if p.strip()]
            if len(parts) >= 2:
                u_cand, p_cand = parts[0], parts[1]
                stmt_up = (
                    select(Voucher)
                    .where(Voucher.username == u_cand, Voucher.password == p_cand)
                    .options(selectinload(Voucher.product))
                    .with_for_update()
                )
                voucher = (await self.session.execute(stmt_up)).scalar_one_or_none()

        # 3. If still not found, check by username alone
        if not voucher:
            stmt_u = (
                select(Voucher)
                .where(Voucher.username == raw_clean)
                .options(selectinload(Voucher.product))
                .with_for_update()
            )
            voucher = (await self.session.execute(stmt_u)).scalar_one_or_none()

        if not voucher:
            return {
                "success": False,
                "error": "NOT_FOUND",
                "message": "❌ کد کارت ووچر یا نام کاربری و رمز عبور وارد شده در سیستم یافت نشد.\nلطفاً اطلاعات را مجدداً بررسی فرمایید.",
            }

        if voucher.is_used:
            used_time_str = ""
            if voucher.used_at:
                used_time_str = voucher.used_at.strftime("%Y-%m-%d %H:%M")
            return {
                "success": False,
                "error": "ALREADY_USED",
                "message": f"⚠️ این کارت ووچر قبلاً استفاده شده است!{f' (در تاریخ: {used_time_str})' if used_time_str else ''}\nهر کارت فقط ۱ بار قابل فعال‌سازی می‌باشد.",
            }

        now = datetime.now(timezone.utc)
        if voucher.expires_at and voucher.expires_at < now:
            return {
                "success": False,
                "error": "EXPIRED",
                "message": "⚠️ مهلت استفاده از این کارت ووچر به پایان رسیده و منقضی شده است.",
            }

        # --- Process by voucher type ---
        if voucher.voucher_type == "wallet_credit":
            amount_toman = voucher.amount_toman
            amount_irr = amount_toman * 10
            wallet_service = WalletService(self.session)
            new_balance_irr = await wallet_service.credit(
                user_id=user.id,
                amount_irr=amount_irr,
                payment_id=f"VOUCHER:{voucher.code}",
                reason="VOUCHER_REDEMPTION",
            )

            voucher.is_used = True
            voucher.used_at = now
            voucher.used_by_telegram_id = user.telegram_id
            await self.session.flush()

            logger.info(
                f"User {user.telegram_id} redeemed credit voucher {voucher.code} for {amount_toman:,} Toman."
            )

            return {
                "success": True,
                "type": "wallet_credit",
                "amount_toman": amount_toman,
                "new_balance_toman": new_balance_irr // 10,
                "voucher": voucher,
                "message": (
                    f"🎉 <b>کارت هدیه با موفقیت فعال شد!</b> 💳✨\n\n"
                    f"💰 مبلغ <b>{amount_toman:,} تومان</b> به کیف پول حساب شما افزوده شد.\n"
                    f"💼 موجودی جدید کیف پول: <b>{new_balance_irr // 10:,} تومان</b>\n\n"
                    f"هم‌اکنون می‌توانید از این موجودی جهت خرید یا تمدید اشتراک‌های خود استفاده فرمایید."
                ),
            }

        elif voucher.voucher_type == "plan":
            product = voucher.product
            if not product and voucher.product_id:
                try:
                    product = await self.session.get(Product, voucher.product_id)
                except Exception:
                    pass

            # Auto-match product by duration or pick first active product
            if not product:
                target_days = voucher.duration_days or calculate_duration_days(voucher.duration_months or 1)
                stmt_match = (
                    select(Product)
                    .where(Product.is_active == True)
                    .order_by(func.abs(Product.duration_days - target_days), Product.sort_order.asc())
                )
                product = (await self.session.execute(stmt_match)).scalars().first()

            if not product:
                # Fallback to any active product
                product = (await self.session.execute(
                    select(Product).where(Product.is_active == True).order_by(Product.sort_order.asc())
                )).scalars().first()

            if not product:
                return {
                    "success": False,
                    "error": "PRODUCT_NOT_FOUND",
                    "message": "❌ هیچ پلن یا محصول فعالی در سیستم یافت نشد. لطفاً با پشتیبانی تماس بگیرید.",
                }

            # Exact days to grant: honoring 12 months = 365 days fix
            days_to_grant = voucher.duration_days or calculate_duration_days(voucher.duration_months or 1)
            months_display = voucher.duration_months or (12 if days_to_grant == 365 else max(1, days_to_grant // 30))

            # Create Order
            order = Order(
                id=uuid.uuid4(),
                user_id=user.id,
                product_id=product.id,
                total_price_irr=product.price_irr,
                status=OrderStatus.PAID,
            )
            self.session.add(order)
            await self.session.flush()

            # Provision Subscription
            try:
                sub_service = SubscriptionService(self.session)
                subscription = await sub_service.provision_subscription_for_order(
                    order_id=order.id,
                    duration_days_override=days_to_grant,
                )
                conn = await sub_service.get_connection_details(subscription.id)
            except Exception as exc:
                logger.error(f"Error provisioning voucher plan subscription: {exc}", exc_info=True)
                order.status = OrderStatus.PROVISIONING_FAILED
                order.provisioning_error = str(exc)
                await self.session.flush()
                return {
                    "success": False,
                    "error": "PROVISION_FAILED",
                    "message": f"❌ خطا در فعال‌سازی و ساخت اشتراک در سرور: {exc}\nلطفاً موضوع را به پشتیبانی اطلاع دهید.",
                }

            voucher.is_used = True
            voucher.used_at = now
            voucher.used_by_telegram_id = user.telegram_id
            await self.session.flush()

            logger.info(
                f"User {user.telegram_id} redeemed plan voucher {voucher.code} (months: {months_display}, days: {days_to_grant}). Subscription ID: {subscription.id}"
            )

            return {
                "success": True,
                "type": "plan",
                "product": product,
                "subscription": subscription,
                "conn": conn,
                "voucher": voucher,
                "message": (
                    f"🎉 <b>کارت اشتراک با موفقیت فعال شد!</b> 🌐✨\n\n"
                    f"📦 طرح: <b>{product.name}</b>\n"
                    f"⏳ مدت اعتبار: <b>{days_to_grant} روز ({months_display} ماه کامل)</b>\n"
                    f"📊 حجم مجاز: <b>{product.traffic_display}</b>\n"
                    f"🌍 لوکیشن: <b>{conn.get('flag', '')} {conn.get('location', '')}</b>\n"
                    f"🔒 پروتکل: <b>{conn.get('protocol', 'VLESS')}</b>\n\n"
                    f"🔗 <b>لینک هوشمند سابسکریپشن:</b>\n"
                    f"<code>{conn['subscription_url']}</code>\n\n"
                    f"📱 <b>نرم‌افزار پیشنهادی (Happ Proxy):</b>\n"
                    f"🍏 <a href=\"https://apps.apple.com/app/happ-proxy-utility/id6504287215\">دانلود نسخه iOS (آیفون)</a>\n"
                    f"🤖 <a href=\"https://play.google.com/store/apps/details?id=com.happproxy\">دانلود نسخه اندروید</a>\n\n"
                    f"💡 <i>لینک فوق را کپی فرمایید یا بارکد QR بالا را درون نرم‌افزار اسکن نمایید.</i>"
                ),
            }

        return {
            "success": False,
            "error": "UNKNOWN_TYPE",
            "message": "❌ نوع ووچر تعریف نشده است.",
        }

    async def delete_used_vouchers(self) -> int:
        """Purge all used vouchers (is_used == True) from DB."""
        stmt = delete(Voucher).where(Voucher.is_used == True)
        res = await self.session.execute(stmt)
        await self.session.flush()
        return res.rowcount or 0

    async def get_unused_vouchers_for_export(self) -> List[Dict[str, Any]]:
        """Fetch all unused vouchers formatted for Excel export."""
        stmt = (
            select(Voucher)
            .where(Voucher.is_used == False)
            .order_by(Voucher.created_at.desc())
        )
        vouchers = (await self.session.execute(stmt)).scalars().all()
        return [
            {
                "duration_months": v.duration_months or 1,
                "username": v.username or "",
                "password": v.password or "",
                "code": v.code,
                "duration_days": v.duration_days or 30,
            }
            for v in vouchers
        ]
