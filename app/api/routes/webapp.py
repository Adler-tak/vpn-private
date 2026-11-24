"""Telegram Mini App (WebApp) backend routes."""
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Depends, Query, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.database.models.user import User, Wallet
from app.database.models.subscription import Subscription
from app.database.models.product import Product
from app.database.models.panel import Panel, Server
from app.core.config import settings, get_live_env
from app.core.constants import SubscriptionStatus
from app.services.settings_service import get_effective_bot_username
from app.services.lucky_wheel_service import LuckyWheelService
from app.services.gift_card_service import GiftCardService
from app.modules.vouchers.service import VoucherService
from app.services.flash_sale_service import FlashSaleService
from app.services.emergency_grace_service import EmergencyGraceService

logger = logging.getLogger(__name__)

router = APIRouter()


class RenewWebRequest(BaseModel):
    subscription_id: str = Field(..., description="UUID of the subscription to renew or upgrade")
    product_id: str = Field(..., description="UUID of the product package selected")
    telegram_id: Optional[int] = Field(None, description="Telegram ID of the customer")


class EmergencyGraceRequest(BaseModel):
    subscription_id: str = Field(..., description="UUID of the subscription to receive 24h grace")
    telegram_id: Optional[int] = Field(None, description="Telegram ID of the customer")


@router.get("/user-data")
async def get_webapp_user_data(
    tg_id: Optional[int] = Query(None, description="Telegram user ID"),
    token: Optional[str] = Query(None, description="Subscription token or direct token"),
    sub_id: Optional[str] = Query(None, description="Direct subscription UUID"),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Fetch profile, active subscriptions, and renewal packages for Telegram Mini App."""
    user = None
    target_sub = None

    # 1. Resolve user by Telegram ID
    if tg_id:
        user_res = await db.execute(select(User).where(User.telegram_id == tg_id))
        user = user_res.scalars().first()

    # 2. Resolve by sub_id or token if user not resolved yet
    if not user and sub_id:
        try:
            s_uuid = uuid.UUID(sub_id)
            sub_res = await db.execute(select(Subscription).where(Subscription.id == s_uuid))
            target_sub = sub_res.scalars().first()
            if target_sub:
                user = await db.get(User, target_sub.user_id)
        except Exception:
            pass

    if not user and token:
        sub_res = await db.execute(select(Subscription).where(Subscription.subscription_token == token))
        target_sub = sub_res.scalars().first()
        if target_sub:
            user = await db.get(User, target_sub.user_id)

    # 3. Fetch all active subscriptions for the user
    subs_data = []
    if user:
        subs_query = await db.execute(
            select(Subscription)
            .where(
                and_(
                    Subscription.user_id == user.id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                )
            )
            .order_by(Subscription.created_at.desc())
        )
        all_subs = list(subs_query.scalars().all())

        now = datetime.now(timezone.utc)
        for s in all_subs:
            limit_b = s.traffic_limit_bytes or 0
            used_b = s.traffic_used_bytes or 0
            limit_gb = round(limit_b / (1024**3), 2) if limit_b > 0 else 0
            used_gb = round(used_b / (1024**3), 2)
            rem_gb = round(max(0, limit_b - used_b) / (1024**3), 2) if limit_b > 0 else 999.0

            usage_pct = 0
            if limit_b > 0:
                usage_pct = min(100, int((used_b / limit_b) * 100))

            rem_days = 0
            if s.expires_at:
                exp = s.expires_at if s.expires_at.tzinfo else s.expires_at.replace(tzinfo=timezone.utc)
                diff = exp - now
                rem_days = max(0, diff.days)

            # Build direct sub URL
            sub_url = f"{settings.SERVER_HOST}/sub/{s.subscription_token}"

            subs_data.append({
                "id": str(s.id),
                "client_email": s.client_email,
                "protocol": (s.protocol or "vless").upper(),
                "status": s.status,
                "used_gb": used_gb,
                "limit_gb": limit_gb,
                "remaining_gb": rem_gb,
                "usage_percentage": usage_pct,
                "auto_renew": bool(getattr(s, "auto_renew", False)),
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "remaining_days": rem_days,
                "subscription_url": sub_url,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            })

    # 4. Fetch available products (packages) and flash sale status
    flash_sale_info = FlashSaleService.get_flash_sale_info()
    is_flash = flash_sale_info.get("is_active", False)
    flash_pct = flash_sale_info.get("discount_percent", 0)

    products_res = await db.execute(
        select(Product)
        .where(Product.is_active == True)
        .order_by(Product.sort_order.asc(), Product.price_irr.asc())
    )
    products = list(products_res.scalars().all())
    products_data = [
        {
            "id": str(p.id),
            "name": p.name,
            "description": p.description or "",
            "traffic_gb": p.traffic_gb,
            "duration_days": p.duration_days,
            "price_toman": p.price_toman,
            "final_price_toman": FlashSaleService.calculate_discounted_price(p.price_toman) if is_flash else p.price_toman,
            "discount_percent": flash_pct if is_flash else 0,
            "is_featured": getattr(p, "is_featured", False),
        }
        for p in products
    ]

    effective_bot = await get_effective_bot_username(db)

    wallet_balance = 0
    if user:
        wallet = user.wallet
        if not wallet:
            w_res = await db.execute(select(Wallet).where(Wallet.user_id == user.id))
            wallet = w_res.scalars().first()
        if wallet:
            wallet_balance = wallet.balance_toman

    return {
        "success": True,
        "user": {
            "id": str(user.id) if user else None,
            "telegram_id": user.telegram_id if user else (tg_id or 0),
            "first_name": user.first_name if user else "کاربر گرامی",
            "username": user.username if user else "",
            "balance_toman": wallet_balance,
            "role": user.role if user else "user",
        } if user else None,
        "subscriptions": subs_data,
        "products": products_data,
        "flash_sale": flash_sale_info,
        "bot_username": effective_bot.lstrip("@"),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/renew")
async def webapp_renew_subscription(
    req: RenewWebRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Renew or upgrade a subscription via wallet balance directly inside the Mini App."""
    try:
        sub_uuid = uuid.UUID(req.subscription_id)
        prod_uuid = uuid.UUID(req.product_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="شناسه اشتراک یا پکیج نامعتبر است.")

    sub = await db.get(Subscription, sub_uuid)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک مورد نظر یافت نشد.")

    user = await db.get(User, sub.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر مالک اشتراک یافت نشد.")

    product = await db.get(Product, prod_uuid)
    if not product or not product.is_active:
        raise HTTPException(status_code=400, detail="بسته انتخابی فعال نیست.")

    # Calculate effective price with flash sale discount if active
    effective_price_toman = FlashSaleService.calculate_discounted_price(product.price_toman)

    # Check wallet balance
    wallet = user.wallet
    if not wallet:
        w_res = await db.execute(select(Wallet).where(Wallet.user_id == user.id))
        wallet = w_res.scalars().first()
    
    current_balance = wallet.balance_toman if wallet else 0
    if current_balance < effective_price_toman:
        diff = effective_price_toman - current_balance
        return {
            "success": False,
            "needs_topup": True,
            "message": f"موجودی کیف پول شما کافی نیست. کسری: {diff:,} تومان",
            "required_toman": effective_price_toman,
            "current_balance": current_balance,
            "shortage_toman": diff,
        }

    # Deduct balance in IRR (Toman * 10)
    if wallet:
        wallet.balance_irr -= (effective_price_toman * 10)

    # Add traffic & duration to subscription
    now = datetime.now(timezone.utc)
    add_traffic_bytes = product.traffic_bytes or int(product.traffic_gb * (1024**3))
    sub.traffic_limit_bytes = (sub.traffic_limit_bytes or 0) + add_traffic_bytes

    current_expiry = sub.expires_at or now
    if current_expiry.tzinfo is None:
        current_expiry = current_expiry.replace(tzinfo=timezone.utc)
    base_date = max(now, current_expiry)
    from datetime import timedelta
    sub.expires_at = base_date + timedelta(days=product.duration_days)
    sub.status = SubscriptionStatus.ACTIVE
    sub.alert_traffic_sent = False
    sub.alert_time_sent = False

    await db.commit()

    logger.info(
        f"MiniApp Renew: User {user.id} renewed sub {sub.id} with product {product.name} ({product.price_toman} Toman)"
    )

    return {
        "success": True,
        "message": f"اشتراک شما با موفقیت با بسته «{product.name}» تمدید/ارتقا یافت.",
        "new_balance": wallet.balance_toman if wallet else 0,
        "remaining_gb": sub.remaining_gb,
        "expires_at": sub.expires_at.isoformat(),
    }


# =========================================================================
# LUCKY WHEEL WEBAPP ENDPOINTS
# =========================================================================

class SpinWheelRequest(BaseModel):
    telegram_id: Optional[int] = None
    user_id: Optional[str] = None


async def _resolve_user_helper(
    tg_id: Optional[int],
    u_id_str: Optional[str],
    db: AsyncSession,
) -> Optional[User]:
    if tg_id:
        user_res = await db.execute(select(User).where(User.telegram_id == tg_id))
        user = user_res.scalars().first()
        if user:
            return user
    if u_id_str:
        try:
            u_uuid = uuid.UUID(u_id_str)
            return await db.get(User, u_uuid)
        except Exception:
            pass
    return None


@router.get("/lucky-wheel")
async def webapp_get_lucky_wheel(
    tg_id: Optional[int] = Query(None),
    user_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Get lucky wheel status, countdown timer, and prize segments for current user."""
    user = await _resolve_user_helper(tg_id, user_id, db)
    if not user:
        cfg = await LuckyWheelService.get_config(db)
        return {
            "success": True,
            "is_enabled": cfg.get("is_enabled", True),
            "can_spin": False,
            "remaining_seconds": 0,
            "message": "جهت چرخاندن گردونه لطفاً وارد حساب تلگرام خود شوید.",
            "prizes": cfg.get("prizes", []),
        }

    status_data = await LuckyWheelService.get_user_status(user.id, db)
    return {
        "success": True,
        **status_data,
    }


@router.post("/lucky-wheel/spin")
async def webapp_spin_lucky_wheel(
    req: SpinWheelRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Execute wheel spin, calculate prize, deliver reward, and update user balance."""
    user = await _resolve_user_helper(req.telegram_id, req.user_id, db)
    if not user:
        raise HTTPException(status_code=401, detail="کاربر احراز هویت نشد. لطفاً از طریق ربات تلگرام وارد شوید.")

    res = await LuckyWheelService.spin(user=user, db=db)
    return res


# =========================================================================
# TELEGRAM GIFT CARD WEBAPP ENDPOINTS
# =========================================================================

class CreateGiftCardWebRequest(BaseModel):
    product_id: str = Field(..., description="Package UUID to gift")
    sender_name: Optional[str] = Field("یک دوست عزیز", description="Name of the gift sender")
    gift_note: Optional[str] = Field("یک هدیه ویژه اینترنت پرسرعت 🎁", description="Personalized greeting note")
    telegram_id: Optional[int] = None
    user_id: Optional[str] = None


class RedeemGiftWebRequest(BaseModel):
    code: str = Field(..., description="Voucher or Gift Card code")
    telegram_id: Optional[int] = None
    user_id: Optional[str] = None


@router.post("/gift-card/create")
async def webapp_create_gift_card(
    req: CreateGiftCardWebRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Buy a Telegram gift subscription card using wallet balance."""
    user = await _resolve_user_helper(req.telegram_id, req.user_id, db)
    if not user:
        raise HTTPException(status_code=401, detail="کاربر احراز هویت نشد.")

    try:
        p_uuid = uuid.UUID(req.product_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="شناسه پکیج نامعتبر است.")

    try:
        res = await GiftCardService.create_gift_card(
            user=user,
            product_id=p_uuid,
            sender_name=req.sender_name or "یک دوست عزیز",
            gift_note=req.gift_note or "هدیه اینترنت پرسرعت",
            db=db,
        )
        return res
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as exc:
        logger.error(f"Error creating gift card in webapp: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="خطا در ساخت کارت هدیه تلگرامی.")


@router.get("/gift-card/{code}")
async def webapp_get_gift_card_info(
    code: str,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """View gift card greeting and unboxing status."""
    info = await GiftCardService.get_gift_info(code=code, db=db)
    return info


@router.post("/gift-card/redeem")
async def webapp_redeem_gift_card(
    req: RedeemGiftWebRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Redeem a gift card or voucher code directly within Mini App."""
    user = await _resolve_user_helper(req.telegram_id, req.user_id, db)
    if not user:
        raise HTTPException(status_code=401, detail="جهت فعال‌سازی باید وارد حساب شوید.")

    voucher_service = VoucherService(db)
    res = await voucher_service.redeem_voucher(raw_code=req.code, user=user)
    return res


@router.get("/flash-sale")
async def webapp_get_flash_sale() -> Dict[str, Any]:
    """Get active flash sale countdown and details."""
    return FlashSaleService.get_flash_sale_info()


@router.post("/emergency-grace")
async def webapp_activate_emergency_grace(
    req: EmergencyGraceRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Activate 24-hour, 1 GB emergency grace period for an expired or depleted subscription."""
    try:
        sub_uuid = uuid.UUID(req.subscription_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="شناسه اشتراک نامعتبر است.")

    sub = await db.get(Subscription, sub_uuid)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک مورد نظر یافت نشد.")

    # Validate user ownership
    user = await db.get(User, sub.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="کاربر مالک اشتراک یافت نشد.")

    if req.telegram_id and user.telegram_id != req.telegram_id:
        raise HTTPException(status_code=403, detail="شما دسترسی به این اشتراک ندارید.")

    result = await EmergencyGraceService.activate_emergency_grace(
        subscription_id=sub.id,
        user_id=user.id,
        session=db,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "امکان فعال‌سازی اتصال اضطراری وجود ندارد."))

    return result


class ToggleAutoRenewRequest(BaseModel):
    subscription_id: str = Field(..., description="UUID of the subscription")
    telegram_id: Optional[int] = Field(None, description="Telegram ID of the customer")
    user_id: Optional[str] = Field(None, description="User UUID")


@router.post("/auto-renew/toggle")
async def webapp_toggle_auto_renew(
    req: ToggleAutoRenewRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Toggle auto-renew flag on user's subscription."""
    from app.services.auto_renew_service import AutoRenewService
    try:
        sub_uuid = uuid.UUID(req.subscription_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="شناسه اشتراک نامعتبر است.")

    sub = await db.get(Subscription, sub_uuid)
    if not sub:
        raise HTTPException(status_code=404, detail="اشتراک مورد نظر یافت نشد.")

    user = await _resolve_user_helper(req.telegram_id, req.user_id, db)
    if not user or user.id != sub.user_id:
        raise HTTPException(status_code=403, detail="شما دسترسی به این اشتراک ندارید.")

    result = await AutoRenewService.toggle_auto_renew(
        subscription_id=sub.id,
        user_id=user.id,
        session=db,
    )
    return result


