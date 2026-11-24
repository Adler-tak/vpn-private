"""Telegram Gift Subscription Card Service."""
import logging
import secrets
import uuid
from typing import Optional, Dict, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models.voucher import Voucher
from app.database.models.product import Product
from app.database.models.user import User, Wallet
from app.modules.vouchers.service import VoucherService, VOUCHER_ALPHABET, normalize_voucher_code
from app.services.settings_service import get_effective_bot_username

logger = logging.getLogger(__name__)


def generate_gift_code() -> str:
    """Generate a clean and memorable gift card code: GIFT-XXXX-XXXX."""
    p1 = "".join(secrets.choice(VOUCHER_ALPHABET) for _ in range(4))
    p2 = "".join(secrets.choice(VOUCHER_ALPHABET) for _ in range(4))
    return f"GIFT-{p1}-{p2}"


class GiftCardService:
    """Service to create and redeem specialized Telegram Gift Subscription Cards."""

    @classmethod
    async def create_gift_card(
        cls,
        user: User,
        product_id: uuid.UUID,
        sender_name: str,
        gift_note: str,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """Purchase a gift subscription voucher using wallet balance with personalized greetings."""
        product = await db.get(Product, product_id)
        if not product or not product.is_active:
            raise ValueError("بسته انتخابی نامعتبر یا غیرفعال است.")

        # Check wallet balance
        w_res = await db.execute(select(Wallet).where(Wallet.user_id == user.id))
        wallet = w_res.scalars().first()

        current_balance = wallet.balance_toman if wallet else 0
        if current_balance < product.price_toman:
            diff = product.price_toman - current_balance
            return {
                "success": False,
                "needs_topup": True,
                "message": f"موجودی کیف پول شما کافی نیست. کسری: {diff:,} تومان",
                "required_toman": product.price_toman,
                "current_balance": current_balance,
                "shortage_toman": diff,
            }

        # Deduct wallet balance
        if wallet:
            wallet.balance_irr -= (product.price_toman * 10)

        # Generate unique code
        code = generate_gift_code()
        # Verify uniqueness
        while (await db.execute(select(Voucher).where(Voucher.code == code))).scalar_one_or_none():
            code = generate_gift_code()

        clean_sender = (sender_name or user.first_name or "یک دوست عزیز").strip()[:64]
        clean_note = (gift_note or "یک هدیه ویژه اینترنت پرسرعت و بدون محدودیت برای شما 🎁").strip()[:200]
        greeting_meta = f"{clean_sender} | {clean_note}"

        voucher = Voucher(
            id=uuid.uuid4(),
            code=code,
            username=greeting_meta[:128],
            password=None,
            duration_months=1 if product.duration_days <= 31 else (product.duration_days // 30),
            duration_days=product.duration_days,
            voucher_type="plan",
            amount_toman=product.price_toman,
            product_id=product.id,
            is_used=False,
            batch_id="telegram_gift",
        )
        db.add(voucher)
        await db.commit()

        # Dynamic bot username
        raw_bot_user = await get_effective_bot_username(db)
        bot_user = raw_bot_user.lstrip("@")

        deep_link = f"https://t.me/{bot_user}?start=gift_{code}"
        share_text = (
            f"🎁✨ *کارت هدیه اشتراک VPN Private* ✨🎁\n\n"
            f"💌 *از طرف:* {clean_sender}\n"
            f"📝 *پیام:* {clean_note}\n\n"
            f"📦 *بسته هدیه:* {product.name} ({product.traffic_gb} گیگابایت - {product.duration_days} روزه)\n\n"
            f"👇 *جهت فعال‌سازی آنی و دریافت کانفیگ اتصال، روی لینک زیر کلیک کنید:*\n"
            f"{deep_link}"
        )

        logger.info(
            f"GiftCard created: User {user.id} bought gift {code} ({product.name}) for '{clean_sender}'"
        )

        return {
            "success": True,
            "code": code,
            "deep_link": deep_link,
            "product_name": product.name,
            "traffic_gb": product.traffic_gb,
            "duration_days": product.duration_days,
            "price_toman": product.price_toman,
            "sender_name": clean_sender,
            "gift_note": clean_note,
            "share_text": share_text,
            "new_balance_toman": wallet.balance_toman if wallet else 0,
        }

    @classmethod
    async def get_gift_info(cls, code: str, db: AsyncSession) -> Dict[str, Any]:
        """Fetch gift card status and greeting details without redeeming."""
        raw_clean = (code or "").strip()
        cand = normalize_voucher_code(raw_clean)
        stmt = (
            select(Voucher)
            .where((Voucher.code == cand) | (Voucher.code == raw_clean.upper()))
            .options(selectinload(Voucher.product))
        )
        voucher = (await db.execute(stmt)).scalars().first()
        if not voucher:
            return {"exists": False, "message": "کارت هدیه مورد نظر یافت نشد."}

        sender_name = "یک دوست عزیز"
        gift_note = ""
        if voucher.username and "|" in voucher.username:
            parts = voucher.username.split("|", 1)
            sender_name = parts[0].strip()
            gift_note = parts[1].strip()
        elif voucher.username:
            sender_name = voucher.username.strip()

        prod_name = voucher.product.name if voucher.product else "اشتراک اختصاصی"
        traffic_gb = voucher.product.traffic_gb if voucher.product else 0

        return {
            "exists": True,
            "code": voucher.code,
            "is_used": voucher.is_used,
            "used_at": voucher.used_at.isoformat() if voucher.used_at else None,
            "product_name": prod_name,
            "traffic_gb": traffic_gb,
            "duration_days": voucher.duration_days,
            "sender_name": sender_name,
            "gift_note": gift_note,
        }
