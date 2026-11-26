"""Order processing and payment settlement workflow."""
from datetime import datetime, timezone
import logging
import uuid
import secrets
from typing import Optional, Tuple
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.order import Order
from app.database.models.product import Product
from app.database.models.user import User
from app.database.models.payment import (
    Payment,
    PaymentReceipt,
    PaymentGateway,
    get_active_gateway_for_provider,
    get_active_gateways,
)
from app.database.models.reseller import Coupon, Referral
from app.services.reseller_service import ResellerService
from app.database.models.subscription import Subscription
from app.database.models.audit import AuditLog
from app.modules.accounting.service import AccountingService
from app.modules.subscriptions.service import SubscriptionService
from app.modules.wallets.service import WalletService
from app.integrations.payments.card_to_card import CardToCardGateway
from app.integrations.payments.zarinpal import ZarinpalGateway
from app.integrations.payments.sep import SEPGateway
from app.integrations.payments.idpay import IDPayGateway
from app.integrations.payments.base import PaymentInitResult
from app.core.constants import OrderStatus, PaymentMethod, PaymentStatus, UserRole
from app.core.config import settings
from app.core.exceptions import VPNPrivateError, OrderStateError

logger = logging.getLogger(__name__)


class OrderService:
    """Manages order creation, discount calculation, payment dispatch, and provisioning."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.accounting = AccountingService(session)
        self.subscription_service = SubscriptionService(session)
        self.wallet_service = WalletService(session)
        self.c2c_gateway = CardToCardGateway()
        self.zarinpal_gateway = ZarinpalGateway()

    async def create_order(
        self,
        user_id: uuid.UUID,
        product_id: uuid.UUID,
        server_id: Optional[uuid.UUID] = None,
        coupon_code: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Order:
        """Create order and apply discounts."""
        if idempotency_key:
            existing = (
                await self.session.execute(
                    select(Order).where(Order.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if existing:
                return existing

        product = await self.session.get(Product, product_id)
        if not product or not product.is_active:
            raise VPNPrivateError("محصول انتخاب شده فعال یا معتبر نیست.")

        price_irr = product.price_irr
        coupon = None
        if coupon_code:
            now_utc = datetime.now(timezone.utc)
            coupon_res = await self.session.execute(
                select(Coupon)
                .where(
                    and_(
                        Coupon.code == coupon_code.strip().upper(),
                        Coupon.is_active.is_(True),
                        Coupon.current_uses < Coupon.max_uses,
                    )
                )
                .with_for_update()
            )
            candidate = coupon_res.scalar_one_or_none()
            if candidate:
                is_valid = True
                if candidate.valid_until:
                    cand_valid = candidate.valid_until
                    if cand_valid.tzinfo is None:
                        cand_valid = cand_valid.replace(tzinfo=timezone.utc)
                    if cand_valid < now_utc:
                        is_valid = False
                if is_valid:
                    coupon = candidate
                    if coupon.discount_percent > 0:
                        discount = (price_irr * coupon.discount_percent) // 100
                        price_irr = max(0, price_irr - discount)
                    elif coupon.discount_amount_irr > 0:
                        price_irr = max(0, price_irr - coupon.discount_amount_irr)
                    # Note: coupon.current_uses is deferred until payment completion

        user = await self.session.get(User, user_id)
        if user and user.role == UserRole.RESELLER and not coupon:
            reseller_pct = await ResellerService.get_reseller_discount_percent(user.id, self.session)
            reseller_discount = (price_irr * reseller_pct) // 100
            price_irr = max(0, price_irr - reseller_discount)

        order = Order(
            id=uuid.uuid4(),
            user_id=user_id,
            product_id=product_id,
            server_id=server_id,
            total_price_irr=price_irr,
            status=OrderStatus.CREATED,
            coupon_id=coupon.id if coupon else None,
            idempotency_key=idempotency_key,
        )
        self.session.add(order)
        
        try:
            user = await self.session.get(User, user_id)
            actor_tg_id = user.telegram_id if user else None
            audit = AuditLog(
                actor_id=actor_tg_id,
                actor_role="CUSTOMER",
                action="ORDER_CREATED",
                target_type="Order",
                target_id=str(order.id),
                details=f"ثبت سفارش جدید برای محصول «{product.name}» (مبلغ: {order.total_price_irr // 10:,} تومان)",
            )
            self.session.add(audit)
        except Exception as e_audit_order:
            logger.warning(f"Failed to record audit for order {order.id}: {e_audit_order}")

        await self.session.flush()
        return order

    async def initiate_payment(
        self,
        order_id: uuid.UUID,
        method: str,
        callback_url: Optional[str] = None,
    ) -> Tuple[Payment, PaymentInitResult]:
        """Generate payment record and dispatch to payment gateway."""
        order = await self.session.get(Order, order_id)
        if not order:
            raise VPNPrivateError("سفارش یافت نشد.")

        payment_id = uuid.uuid4()
        payment = Payment(
            id=payment_id,
            order_id=order.id,
            user_id=order.user_id,
            amount_irr=order.total_price_irr,
            provider=method,
            status=PaymentStatus.PENDING,
        )
        self.session.add(payment)
        await self.session.flush()

        order.status = OrderStatus.PAYMENT_PENDING
        await self.session.flush()

        if method == PaymentMethod.CARD_TO_CARD:
            init_res = await self.c2c_gateway.request_payment(
                payment_id=str(payment.id),
                amount_irr=payment.amount_irr,
                callback_url="",
                description=f"سفارش شماره {order.id}",
            )
            payment.tracking_code = init_res.tracking_code

        elif method == PaymentMethod.ZARINPAL:
            gw = await get_active_gateway_for_provider(self.session, "zarinpal")
            if gw:
                payment.gateway_id = gw.id
                cfg = gw.config_dict
                z_gw = ZarinpalGateway(
                    merchant_id=cfg.get("merchant_id"),
                    sandbox=cfg.get("sandbox", False),
                )
            else:
                z_gw = self.zarinpal_gateway

            init_res = await z_gw.request_payment(
                payment_id=str(payment.id),
                amount_irr=payment.amount_irr,
                callback_url=callback_url,
                description=f"خرید اشتراک VPN Private - سفارش {order.id}",
            )
            payment.tracking_code = init_res.authority

        elif method == PaymentMethod.SEP:
            gw = await get_active_gateway_for_provider(self.session, "sep")
            tracking_code = f"SEP-{secrets.token_hex(4).upper()}"
            payment.tracking_code = tracking_code

            if gw:
                payment.gateway_id = gw.id
                cfg = gw.config_dict
                sep_gw = SEPGateway(
                    terminal_id=cfg.get("terminal_id"),
                    merchant_id=cfg.get("merchant_id"),
                )
            else:
                sep_gw = SEPGateway()

            user = await self.session.get(User, order.user_id)
            user_phone = getattr(user, "phone", "") if user else ""

            init_res = await sep_gw.request_payment(
                payment_id=str(payment.id),
                amount_irr=payment.amount_irr,
                callback_url=callback_url,
                description=f"خرید اشتراک VPN Private - سفارش {order.id}",
                metadata={"tracking_code": tracking_code, "phone": user_phone},
            )
            if init_res.authority:
                payment.admin_notes = f"TOKEN:{init_res.authority}"
                payment.reference_id = init_res.authority

        elif method == PaymentMethod.IDPAY:
            gw = await get_active_gateway_for_provider(self.session, "idpay")
            tracking_code = f"IDP-{secrets.token_hex(4).upper()}"
            payment.tracking_code = tracking_code

            if gw:
                payment.gateway_id = gw.id
                cfg = gw.config_dict
                idp_gw = IDPayGateway(
                    api_key=cfg.get("api_key"),
                    sandbox=cfg.get("sandbox", False),
                )
            else:
                idp_gw = IDPayGateway()

            user = await self.session.get(User, order.user_id)
            user_name = user.first_name if user else "کاربر"
            user_phone = getattr(user, "phone", "") if user else ""

            init_res = await idp_gw.request_payment(
                payment_id=str(payment.id),
                amount_irr=payment.amount_irr,
                callback_url=callback_url,
                description=f"خرید اشتراک VPN Private - سفارش {order.id}",
                metadata={"tracking_code": tracking_code, "name": user_name, "phone": user_phone},
            )
            if init_res.authority:
                payment.reference_id = init_res.authority

        elif method == PaymentMethod.CRYPTO:
            from app.core.config import settings
            amount_toman = payment.amount_toman
            rate = getattr(settings, "USDT_TOMAN_RATE", 70000)
            usdt_amount = round(amount_toman / rate, 2)
            if usdt_amount < 1.0:
                usdt_amount = 1.0
            tracking_code = f"CRYPTO-{payment.id.hex[:6].upper()}"
            payment.tracking_code = tracking_code

            # Check if active crypto gateway configured in database
            crypto_gw = await get_active_gateway_for_provider(self.session, "crypto")
            if crypto_gw:
                payment.gateway_id = crypto_gw.id
                cfg = crypto_gw.config_dict
                net_name = cfg.get("network", "TRC-20")
                net_wallet = cfg.get("wallet_address", settings.CRYPTO_WALLET_TRC20)
                if cfg.get("rate"):
                    try:
                        rate = int(cfg.get("rate"))
                        usdt_amount = round(amount_toman / rate, 2)
                    except Exception:
                        pass
                net_warn = f"⚠️ <b>توجه مهم:</b> لطفاً انتقال را بر روی شبکه <b>{net_name}</b> انجام دهید."
            else:
                if getattr(settings, "CRYPTO_BEP20_ENABLED", False) and not getattr(settings, "CRYPTO_TRC20_ENABLED", True):
                    net_name = "BEP-20"
                    net_wallet = settings.CRYPTO_WALLET_BEP20
                    net_warn = "⚠️ <b>توجه مهم:</b> لطفاً انتقال را دقیقاً بر روی شبکه <b>BEP-20 (BSC / Binance Smart Chain)</b> انجام دهید."
                else:
                    net_name = "TRC-20"
                    net_wallet = settings.CRYPTO_WALLET_TRC20
                    net_warn = "⚠️ <b>توجه مهم:</b> لطفاً انتقال را دقیقاً بر روی شبکه <b>TRC-20 (Tron)</b> انجام دهید."

            init_res = PaymentInitResult(
                payment_id=str(payment.id),
                tracking_code=tracking_code,
                authority=tracking_code,
                instructions=(
                    f"🪙 <b>اطلاعات پرداخت با ارز دیجیتال (تتر USDT - شبکه {net_name}):</b>\n\n"
                    f"💰 مبلغ دلاری: <b>{usdt_amount} USDT</b>\n"
                    f"💵 معادل تومانی: <b>{amount_toman:,} تومان</b> (نرخ هر تتر: {rate:,} تومان)\n\n"
                    f"📍 <b>آدرس کیف پول تتر ({net_name}):</b>\n<code>{net_wallet}</code>\n\n"
                    f"{net_warn}\n\n"
                    f"📸 پس از پرداخت، لطفاً <b>تصویر رسید یا کد هش (TXID)</b> تراکنش را در همین بخش ارسال نمایید:"
                ),
            )

        elif method == PaymentMethod.PERFECT_MONEY:
            from app.integrations.payments.perfect_money import PerfectMoneyGateway
            pm_gw = await get_active_gateway_for_provider(self.session, "perfect_money")
            if pm_gw:
                payment.gateway_id = pm_gw.id
                cfg = pm_gw.config_dict
                gateway = PerfectMoneyGateway(
                    account_id=cfg.get("account_id"),
                    passphrase=cfg.get("passphrase"),
                    usd_rate=int(cfg.get("usd_rate", 70000)),
                )
            else:
                gateway = PerfectMoneyGateway()

            init_res = await gateway.request_payment(
                payment_id=str(payment.id),
                amount_irr=payment.amount_irr,
                callback_url=callback_url,
                description=f"خرید اشتراک VPN Private - سفارش {order.id}",
            )
            payment.tracking_code = init_res.tracking_code

        elif method == PaymentMethod.STRIPE:
            from app.integrations.payments.stripe_gw import StripeGateway
            stripe_gw = await get_active_gateway_for_provider(self.session, "stripe")
            tracking_code = f"STRP-{secrets.token_hex(4).upper()}"
            payment.tracking_code = tracking_code

            if stripe_gw:
                payment.gateway_id = stripe_gw.id
                cfg = stripe_gw.config_dict
                gateway = StripeGateway(
                    secret_key=cfg.get("secret_key"),
                    publishable_key=cfg.get("publishable_key"),
                    currency=cfg.get("currency", "USD"),
                    exchange_rate_toman=int(cfg.get("exchange_rate", 70000) or 70000),
                )
            else:
                gateway = StripeGateway()

            init_res = await gateway.request_payment(
                payment_id=str(payment.id),
                amount_irr=payment.amount_irr,
                callback_url=callback_url,
                description=f"VPN Private Subscription - Order {str(order.id)[:8]}",
                metadata={"tracking_code": tracking_code},
            )
            if init_res.authority:
                payment.reference_id = init_res.authority

        elif method == PaymentMethod.WALLET or order.total_price_irr <= 0:
            # Immediate deduction from internal wallet (skip if 0 cost due to 100% coupon)
            if order.total_price_irr > 0:
                await self.wallet_service.debit(
                    user_id=order.user_id,
                    amount_irr=order.total_price_irr,
                    order_id=str(order.id),
                )
            payment.status = PaymentStatus.PAID
            payment.paid_at = datetime.now(timezone.utc)
            payment.tracking_code = f"WAL-{payment.id.hex[:8].upper()}"

            # Increment coupon usage upon successful completion
            if order.coupon_id:
                coupon = await self.session.get(Coupon, order.coupon_id)
                if coupon:
                    coupon.current_uses += 1

            await self.session.flush()

            # Provision VPN immediately
            sub = await self.subscription_service.provision_subscription_for_order(order.id)
            await self._process_referral_reward(order)

            init_res = PaymentInitResult(
                payment_id=str(payment.id),
                tracking_code=payment.tracking_code,
                status="PAID",
                instructions="پرداخت سفارش با موفقیت انجام شد.",
            )
            return payment, init_res
        else:
            raise VPNPrivateError(f"روش پرداخت ناشناخته: {method}")

        await self.session.flush()
        return payment, init_res

    async def approve_and_provision_order(
        self,
        payment_id: uuid.UUID,
        admin_id: Optional[int] = None,
        reference_id: Optional[str] = None,
        admin_notes: Optional[str] = None,
    ) -> Optional[Subscription]:
        """Called upon manual Card-to-Card verification or Gateway verification callback."""
        # Row-level lock to prevent concurrent double-credit / double-provisioning
        payment_res = await self.session.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        payment = payment_res.scalar_one_or_none()
        if not payment:
            raise VPNPrivateError("پرداخت یافت نشد.")

        if payment.status == PaymentStatus.PAID:
            logger.warning(f"Payment {payment_id} already marked PAID. Checking subscription status.")
            notes = (payment.admin_notes or "").strip()
            if notes.startswith("RENEWAL:"):
                parts = notes.split(":")
                if len(parts) >= 3:
                    try:
                        sub_uuid = uuid.UUID(parts[1].strip())
                        return await self.session.get(Subscription, sub_uuid)
                    except Exception:
                        pass
            if payment.order_id:
                sub_res = await self.session.execute(
                    select(Subscription).where(Subscription.order_id == payment.order_id)
                )
                existing_sub = sub_res.scalar_one_or_none()
                if existing_sub:
                    return existing_sub
                logger.info(f"Payment {payment_id} is PAID but subscription missing. Retrying provisioning now...")
                subscription = await self.subscription_service.provision_subscription_for_order(payment.order_id)
                await self.session.flush()
                return subscription
            return None

        # Case 0: Subscription Renewal (e.g. Card-to-Card renewal with admin_notes="RENEWAL:<sub_id>:<product_id>")
        notes = (payment.admin_notes or admin_notes or "").strip()
        if notes.startswith("RENEWAL:"):
            parts = notes.split(":")
            if len(parts) >= 3:
                sub_id = uuid.UUID(parts[1].strip())
                product_id = uuid.UUID(parts[2].strip())
                renewed_sub = await self.subscription_service.renew_subscription(sub_id, product_id)

                payment.status = PaymentStatus.PAID
                payment.paid_at = datetime.now(timezone.utc)
                payment.admin_id = admin_id
                if admin_notes:
                    payment.admin_notes = admin_notes
                if reference_id:
                    payment.reference_id = reference_id
                await self.session.flush()
                try:
                    user_renew = await self.session.get(User, payment.user_id)
                    audit_renew = AuditLog(
                        actor_id=user_renew.telegram_id if user_renew else None,
                        actor_role="CUSTOMER",
                        action="SUBSCRIPTION_RENEWED",
                        target_type="Subscription",
                        target_id=str(sub_id),
                        details=f"تمدید موفق اشتراک با پرداخت {payment.amount_irr // 10:,} تومان",
                    )
                    self.session.add(audit_renew)
                except Exception as e_ar:
                    logger.warning(f"Failed to record audit for renewal: {e_ar}")

                logger.info(f"Renewal payment {payment.id} approved for subscription {sub_id} with product {product_id}")
                return renewed_sub

        # Case 1: Direct Wallet Top-up (no Order attached)
        if not payment.order_id:
            payment.status = PaymentStatus.PAID
            payment.paid_at = datetime.now(timezone.utc)
            payment.admin_id = admin_id
            payment.admin_notes = admin_notes
            if reference_id:
                payment.reference_id = reference_id

            await self.wallet_service.credit(
                user_id=payment.user_id,
                amount_irr=payment.amount_irr,
                payment_id=str(payment.id),
                reason="TOPUP",
            )
            try:
                user_top = await self.session.get(User, payment.user_id)
                audit_topup = AuditLog(
                    actor_id=user_top.telegram_id if user_top else None,
                    actor_role="CUSTOMER",
                    action="WALLET_TOPUP",
                    target_type="Wallet",
                    target_id=str(payment.id),
                    details=f"شارژ کیف پول حساب کاربری به مبلغ {payment.amount_irr // 10:,} تومان",
                )
                self.session.add(audit_topup)
            except Exception as e_wt:
                logger.warning(f"Failed to record audit for wallet topup: {e_wt}")

            await self.session.flush()
            logger.info(f"Direct wallet top-up payment {payment.id} approved for user {payment.user_id}")
            return None

        # Case 2: VPN Subscription Order
        order = await self.session.get(Order, payment.order_id)
        if not order:
            raise VPNPrivateError("سفارش مرتبط با این پرداخت وجود ندارد.")

        # Provision VPN Client first (ensuring panel success before finalizing payment)
        subscription = await self.subscription_service.provision_subscription_for_order(order.id)

        # Mark payment PAID
        payment.status = PaymentStatus.PAID
        payment.paid_at = datetime.now(timezone.utc)
        payment.admin_id = admin_id
        payment.admin_notes = admin_notes
        if reference_id:
            payment.reference_id = reference_id

        # Financial Ledger entry
        await self.accounting.record_order_direct_gateway(
            user_id=str(order.user_id),
            order_id=str(order.id),
            amount_irr=payment.amount_irr,
            payment_id=str(payment.id),
        )

        # Increment coupon usage upon order approval and provisioning
        if order.coupon_id:
            coupon = await self.session.get(Coupon, order.coupon_id)
            if coupon:
                coupon.current_uses += 1

        # Check and grant referral commission
        await self._process_referral_reward(order)

        try:
            user_ord = await self.session.get(User, order.user_id)
            prod_name = "اشتراک"
            if order and order.product_id:
                p_obj = await self.session.get(Product, order.product_id)
                if p_obj:
                    prod_name = p_obj.name
            audit_ord = AuditLog(
                actor_id=user_ord.telegram_id if user_ord else None,
                actor_role="CUSTOMER",
                action="ORDER_APPROVED",
                target_type="Subscription",
                target_id=str(subscription.id),
                details=f"خرید و فعال‌سازی موفق اشتراک «{prod_name}» به مبلغ {payment.amount_irr // 10:,} تومان",
            )
            self.session.add(audit_ord)
        except Exception as e_oa:
            logger.warning(f"Failed to record audit for order approval: {e_oa}")

        await self.session.flush()
        logger.info(f"Order {order.id} approved, paid, and provisioned with sub {subscription.id}")
        return subscription

    async def _process_referral_reward(self, order: Order):
        """Award referral commission if user was invited by another customer."""
        user = await self.session.get(User, order.user_id)
        if not user or not user.referred_by_id:
            return

        referrer = await self.session.get(User, user.referred_by_id)
        if not referrer:
            return

        commission_percent = settings.REFERRAL_COMMISSION_PERCENT
        commission_irr = (order.total_price_irr * commission_percent) // 100
        if commission_irr <= 0:
            return

        # Credit referrer wallet
        ref_payment_id = f"REF-{order.id.hex[:8]}"
        await self.wallet_service.credit(
            user_id=referrer.id,
            amount_irr=commission_irr,
            payment_id=ref_payment_id,
            reason="AFFILIATE_COMMISSION",
        )

        # Record referral tracking entity
        referral_record = (
            await self.session.execute(
                select(Referral).where(
                    and_(
                        Referral.referrer_user_id == referrer.id,
                        Referral.referred_user_id == user.id,
                    )
                )
            )
        ).scalar_one_or_none()

        if referral_record:
            referral_record.status = "QUALIFIED"
            referral_record.total_commission_irr += commission_irr
        else:
            referral_record = Referral(
                referrer_user_id=referrer.id,
                referred_user_id=user.id,
                status="QUALIFIED",
                total_commission_irr=commission_irr,
            )
            self.session.add(referral_record)

        logger.info(
            f"Referral reward granted: {commission_irr:,} IRR credited to referrer {referrer.telegram_id}"
        )
