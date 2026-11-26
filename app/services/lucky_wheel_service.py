"""Lucky Wheel and Daily Rewards Service."""
import json
import logging
import random
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.reseller import LuckyWheelSpin, Coupon
from app.database.models.user import User, Wallet
from app.database.models.subscription import Subscription
from app.database.models.panel import Panel, Inbound
from app.database.models.setting import SystemSetting
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

LUCKY_WHEEL_SETTING_KEY = "LUCKY_WHEEL_CONFIG"

DEFAULT_LUCKY_WHEEL_CONFIG: Dict[str, Any] = {
    "is_enabled": True,
    "daily_cooldown_hours": 24,
    "prizes": [
        {
            "id": 1,
            "label": "۵۰۰ مگابایت ترافیک هدیه",
            "type": "traffic_mb",
            "amount": 500,
            "weight": 25,
            "color": "#6366f1",
            "icon": "📶",
        },
        {
            "id": 2,
            "label": "۵,۰۰۰ تومان شارژ کیف پول",
            "type": "wallet_irr",
            "amount": 50000,
            "weight": 20,
            "color": "#10b981",
            "icon": "💰",
        },
        {
            "id": 3,
            "label": "۱ گیگابایت ترافیک هدیه",
            "type": "traffic_mb",
            "amount": 1024,
            "weight": 15,
            "color": "#8b5cf6",
            "icon": "🚀",
        },
        {
            "id": 4,
            "label": "کد تخفیف ۲۰٪",
            "type": "coupon_percent",
            "amount": 20,
            "weight": 15,
            "color": "#f59e0b",
            "icon": "🎟️",
        },
        {
            "id": 5,
            "label": "۱۰,۰۰۰ تومان شارژ کیف پول",
            "type": "wallet_irr",
            "amount": 100000,
            "weight": 10,
            "color": "#ec4899",
            "icon": "💎",
        },
        {
            "id": 6,
            "label": "پوچ! فردا دوباره تلاش کن",
            "type": "empty",
            "amount": 0,
            "weight": 15,
            "color": "#64748b",
            "icon": "🍀",
        },
    ],
}


class LuckyWheelService:
    """Service to administer and execute lucky wheel spins and prize allocation."""

    @classmethod
    async def get_config(cls, db: Optional[AsyncSession] = None) -> Dict[str, Any]:
        """Fetch current lucky wheel configuration from DB or return defaults."""
        raw_val = None
        if db:
            stmt = select(SystemSetting).where(SystemSetting.key == LUCKY_WHEEL_SETTING_KEY)
            row = (await db.execute(stmt)).scalars().first()
            if row and row.value:
                raw_val = row.value

        if not raw_val:
            raw_val = SettingsService.get_value(LUCKY_WHEEL_SETTING_KEY, None)

        if raw_val:
            try:
                cfg = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
                if isinstance(cfg, dict) and "prizes" in cfg and cfg["prizes"]:
                    return cfg
            except Exception as e:
                logger.warning(f"Error parsing lucky wheel config JSON: {e}")

        return DEFAULT_LUCKY_WHEEL_CONFIG

    @classmethod
    async def save_config(cls, config: Dict[str, Any], db: AsyncSession) -> Dict[str, Any]:
        """Validate and persist lucky wheel configuration."""
        if not isinstance(config, dict) or "prizes" not in config or not config["prizes"]:
            raise ValueError("تنظیمات گردونه باید شامل حداقل یک جایزه با شانس معتبر باشد.")

        # Ensure valid fields
        sanitized_prizes = []
        for idx, p in enumerate(config["prizes"], start=1):
            sanitized_prizes.append({
                "id": p.get("id", idx),
                "label": str(p.get("label", f"جایزه {idx}")).strip(),
                "type": p.get("type", "empty"),
                "amount": int(p.get("amount", 0)),
                "weight": max(1, int(p.get("weight", 10))),
                "color": str(p.get("color", "#6366f1")).strip(),
                "icon": str(p.get("icon", "🎁")).strip(),
            })

        sanitized_config = {
            "is_enabled": bool(config.get("is_enabled", True)),
            "daily_cooldown_hours": max(1, int(config.get("daily_cooldown_hours", 24))),
            "prizes": sanitized_prizes,
        }

        json_str = json.dumps(sanitized_config, ensure_ascii=False)
        await SettingsService.set_value(
            key=LUCKY_WHEEL_SETTING_KEY,
            value=json_str,
            category="marketing",
            description="Dynamic options and weights for Daily Lucky Wheel",
            session=db,
        )
        await db.commit()
        return sanitized_config

    @classmethod
    async def get_user_status(cls, user_id: uuid.UUID, db: AsyncSession) -> Dict[str, Any]:
        """Check if user can spin today and return remaining cooldown seconds."""
        cfg = await cls.get_config(db)
        is_enabled = cfg.get("is_enabled", True)
        cooldown_hours = cfg.get("daily_cooldown_hours", 24)

        if not is_enabled:
            return {
                "is_enabled": False,
                "can_spin": False,
                "remaining_seconds": 0,
                "message": "گردونه شانس در حال حاضر غیرفعال است.",
                "prizes": cfg.get("prizes", []),
            }

        stmt = (
            select(LuckyWheelSpin)
            .where(LuckyWheelSpin.user_id == user_id)
            .order_by(LuckyWheelSpin.created_at.desc())
        )
        last_spin = (await db.execute(stmt)).scalars().first()

        now = datetime.now(timezone.utc)
        can_spin = True
        remaining_seconds = 0

        if last_spin and last_spin.created_at:
            spin_time = last_spin.created_at
            if spin_time.tzinfo is None:
                spin_time = spin_time.replace(tzinfo=timezone.utc)
            diff = (now - spin_time).total_seconds()
            required_cooldown = cooldown_hours * 3600
            if diff < required_cooldown:
                can_spin = False
                remaining_seconds = int(required_cooldown - diff)

        return {
            "is_enabled": True,
            "can_spin": can_spin,
            "remaining_seconds": remaining_seconds,
            "last_prize": last_spin.prize_label if last_spin else None,
            "prizes": cfg.get("prizes", []),
        }

    @classmethod
    async def spin(cls, user: User, db: AsyncSession) -> Dict[str, Any]:
        """Execute spin for user, select prize, deliver reward, and save history."""
        status = await cls.get_user_status(user.id, db)
        if not status.get("is_enabled"):
            return {
                "success": False,
                "error": "DISABLED",
                "message": "گردونه شانس در حال حاضر موقتاً غیرفعال است.",
            }

        if not status.get("can_spin"):
            rem = status.get("remaining_seconds", 0)
            hours = rem // 3600
            mins = (rem % 3600) // 60
            return {
                "success": False,
                "error": "COOLDOWN",
                "remaining_seconds": rem,
                "message": f"شما امروز شانس خود را امتحان کرده‌اید! زمان باقیمانده تا شانس بعدی: {hours} ساعت و {mins} دقیقه.",
            }

        cfg = await cls.get_config(db)
        prizes = cfg.get("prizes", [])
        if not prizes:
            return {
                "success": False,
                "error": "NO_PRIZES",
                "message": "هیچ گزینه‌ای در گردونه تنظیم نشده است.",
            }

        weights = [max(1, p.get("weight", 10)) for p in prizes]
        chosen_prize = random.choices(prizes, weights=weights, k=1)[0]

        ptype = chosen_prize.get("type", "empty")
        amount = int(chosen_prize.get("amount", 0))
        label = chosen_prize.get("label", "جایزه")

        reward_detail = ""
        coupon_code = None

        # 1. Deliver Reward
        if ptype == "wallet_irr" and amount > 0:
            w_res = await db.execute(select(Wallet).where(Wallet.user_id == user.id))
            wallet = w_res.scalars().first()
            if not wallet:
                wallet = Wallet(id=uuid.uuid4(), user_id=user.id, balance_irr=0)
                db.add(wallet)
            wallet.balance_irr += amount
            toman_amount = amount // 10
            reward_detail = f"مبلغ {toman_amount:,} تومان به کیف پول شما اضافه شد."

        elif ptype == "traffic_mb" and amount > 0:
            # Add to active subscription
            sub_res = await db.execute(
                select(Subscription)
                .where(Subscription.user_id == user.id, Subscription.status == "ACTIVE")
                .order_by(Subscription.created_at.desc())
            )
            active_sub = sub_res.scalars().first()
            traffic_bytes = amount * 1024 * 1024
            if active_sub:
                if active_sub.traffic_limit_bytes == 0:
                    reward_detail = f"اشتراک شما نامحدود است؛ وضعیت ترافیک نامحدود حفظ شد."
                else:
                    active_sub.traffic_limit_bytes = active_sub.traffic_limit_bytes + traffic_bytes
                    reward_detail = f"حجم {amount} مگابایت به اشتراک فعال شما اضافه شد."
                    try:
                        panel = await db.get(Panel, active_sub.panel_id)
                        inbound = await db.get(Inbound, active_sub.inbound_id)
                        if panel and inbound and panel.is_active:
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
                            expiry_ms = int(active_sub.expires_at.timestamp() * 1000) if active_sub.expires_at else 0
                            spec = ClientSpec(
                                id=active_sub.client_uuid,
                                email=active_sub.client_email,
                                flow="xtls-rprx-vision" if (active_sub.protocol or "").lower() == "vless" else "",
                                limitIp=inbound.max_clients or 2,
                                totalGB=active_sub.traffic_limit_bytes,
                                expiryTime=expiry_ms,
                                enable=True,
                                subId=active_sub.subscription_token,
                            )
                            await adapter.update_client(remote_inb, active_sub.client_uuid, spec)
                    except Exception as sync_err:
                        logger.warning(f"Could not sync lucky wheel traffic to panel: {sync_err}")
            else:
                # Fallback: Credit wallet with equivalent value (e.g. 5,000 Toman)
                w_res = await db.execute(select(Wallet).where(Wallet.user_id == user.id))
                wallet = w_res.scalars().first()
                if not wallet:
                    wallet = Wallet(id=uuid.uuid4(), user_id=user.id, balance_irr=0)
                    db.add(wallet)
                fallback_irr = max(10000, (amount * 50) * 10) # 50 Toman per MB
                wallet.balance_irr += fallback_irr
                reward_detail = f"چون اشتراک فعالی نداشتید، معادل ترافیک به مبلغ {fallback_irr // 10:,} تومان به کیف پول شما واریز شد."

        elif ptype == "coupon_percent" and amount > 0:
            coupon_code = f"WHEEL-{secrets.token_hex(3).upper()}"
            new_coupon = Coupon(
                id=uuid.uuid4(),
                code=coupon_code,
                discount_percent=amount,
                max_uses=1,
                current_uses=0,
                valid_until=datetime.now(timezone.utc) + timedelta(days=7),
                is_active=True,
            )
            db.add(new_coupon)
            reward_detail = f"کد تخفیف اختصاصی شما: <code>{coupon_code}</code> (معتبر تا ۷ روز آینده)"

        elif ptype == "empty":
            reward_detail = "متأسفانه این بار برنده نشدید! فردا حتماً دوباره تلاش کن 🍀"

        # 2. Record Spin History
        spin_record = LuckyWheelSpin(
            id=uuid.uuid4(),
            user_id=user.id,
            telegram_id=user.telegram_id,
            prize_label=label,
            prize_type=ptype,
            prize_amount=amount,
            created_at=datetime.now(timezone.utc),
        )
        db.add(spin_record)
        await db.commit()

        logger.info(
            f"LuckyWheel: User {user.id} ({user.telegram_id}) won '{label}' (type={ptype}, amount={amount})"
        )

        return {
            "success": True,
            "prize": chosen_prize,
            "label": label,
            "type": ptype,
            "amount": amount,
            "reward_detail": reward_detail,
            "coupon_code": coupon_code,
            "message": f"🎉 تبریک! شما برنده «{label}» شدید!\n{reward_detail}" if ptype != "empty" else reward_detail,
        }
