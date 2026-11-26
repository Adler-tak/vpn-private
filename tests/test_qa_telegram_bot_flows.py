"""Comprehensive QA Audit Test Suite for VPN Private Telegram Bot Flows & Business Logic."""
import asyncio
import io
import uuid
import secrets
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database.session import Base
import app.database.models  # ensure all models registered in Base.metadata
from app.database.models.user import User, Wallet
from app.database.models.panel import Panel, Server, Inbound
from app.database.models.product import Product
from app.database.models.order import Order
from app.database.models.subscription import Subscription
from app.database.models.payment import Payment, PaymentReceipt, BankCard
from app.database.models.reseller import Coupon
from app.database.models.voucher import Voucher
from app.database.models.audit import AuditLog
from app.database.models.setting import SystemSetting
from app.core.constants import UserRole, PaymentMethod, PaymentStatus, OrderStatus, SubscriptionStatus
from app.core.security import encrypt_credential
from app.core.config import settings
from app.modules.users.service import UserService
from app.modules.orders.service import OrderService
from app.modules.subscriptions.service import SubscriptionService
from app.modules.vouchers.service import VoucherService, generate_voucher_code, normalize_voucher_code
from app.modules.wallets.service import WalletService
from app.modules.bot_texts.service import get_bot_text, DEFAULT_BOT_TEXTS
from app.services.settings_service import SettingsService
from app.services.gift_card_service import GiftCardService
from app.bot.handlers.start import handle_start, handle_free_trial
from app.bot.handlers.purchase import (
    handle_product_selected,
    handle_skip_coupon,
    handle_coupon_input,
    proceed_to_order_confirmation,
    generate_qr_buffer,
)
from app.bot.states.customer import PurchaseOrderState
from app.bot.handlers.voucher import process_voucher_redemption
from app.bot.handlers.admin import approve_payment_by_admin
from app.bot.keyboards.inline import get_payment_methods_keyboard, get_locations_keyboard


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


# =========================================================================
# MISSION 1: User /start Flow & Admin Notifications
# =========================================================================

@pytest.mark.asyncio
async def test_start_flow_user_registration(db_session: AsyncSession):
    """Verify user registration in DB on start flow, wallet creation, and referral linking."""
    user_service = UserService(db_session)

    # 1. Register referrer user
    referrer, created = await user_service.register_or_get_user(
        telegram_id=111111,
        username="referrer_user",
        first_name="Referrer",
    )
    await db_session.commit()
    assert created is True
    assert referrer.referral_code is not None

    # 2. Register new user with referral code
    new_user, created_new = await user_service.register_or_get_user(
        telegram_id=222222,
        username="new_customer",
        first_name="New",
        last_name="Customer",
        referrer_code=referrer.referral_code,
    )
    await db_session.commit()
    assert created_new is True
    assert new_user.referred_by_id == referrer.id

    # Verify wallet created with 0 balance
    w_res = await db_session.execute(select(Wallet).where(Wallet.user_id == new_user.id))
    wallet = w_res.scalar_one_or_none()
    assert wallet is not None
    assert wallet.balance_irr == 0

    # 3. Returning user interaction (idempotency & info updates)
    returning_user, created_again = await user_service.register_or_get_user(
        telegram_id=222222,
        username="new_customer_updated",
        first_name="NewUpdated",
        last_name="CustomerUpdated",
    )
    await db_session.commit()
    assert created_again is False
    assert returning_user.id == new_user.id
    assert returning_user.username == "new_customer_updated"
    assert returning_user.first_name == "NewUpdated"


@pytest.mark.asyncio
async def test_start_flow_admin_notifications_and_audit_logs(db_session: AsyncSession):
    """Verify handle_start notifies admins and writes AuditLog for both new and returning users."""
    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(
        telegram_id=333333,
        username="audit_user",
        first_name="Audit",
    )
    await db_session.commit()

    mock_bot = AsyncMock()
    mock_message = AsyncMock()
    mock_message.chat.id = 333333
    mock_message.from_user.id = 333333
    mock_message.bot = mock_bot
    mock_message.answer = AsyncMock()

    mock_command = MagicMock()
    mock_command.args = None
    mock_state = AsyncMock()

    # Case A: is_new_user = True
    with patch("app.bot.notifications.notify_all_admins", new_callable=AsyncMock) as mock_notify:
        await handle_start(
            message=mock_message,
            command=mock_command,
            db_user=user,
            db_session=db_session,
            bot=mock_bot,
            state=mock_state,
            is_new_user=True,
        )
        await db_session.commit()

        # Check Audit Log
        audit_res = await db_session.execute(
            select(AuditLog).where(AuditLog.actor_id == 333333, AuditLog.action == "NEW_USER_REGISTERED")
        )
        audit_entry = audit_res.scalar_one_or_none()
        assert audit_entry is not None
        assert "ثبت‌نام و استارت کاربر جدید" in audit_entry.details

        # Check Admin Notification
        assert mock_notify.called
        call_kwargs = mock_notify.call_args.kwargs
        assert "عضو جدید ربات را استارت کرد!" in call_kwargs["text"]
        assert "333333" in call_kwargs["text"]

    # Case B: is_new_user = False (Returning user)
    with patch("app.bot.notifications.notify_all_admins", new_callable=AsyncMock) as mock_notify_ret:
        await handle_start(
            message=mock_message,
            command=mock_command,
            db_user=user,
            db_session=db_session,
            bot=mock_bot,
            state=mock_state,
            is_new_user=False,
        )
        await db_session.commit()

        # Check Audit Log
        audit_res = await db_session.execute(
            select(AuditLog).where(AuditLog.actor_id == 333333, AuditLog.action == "USER_STARTED_BOT")
        )
        audit_entry_ret = audit_res.scalar_one_or_none()
        assert audit_entry_ret is not None
        assert "استارت مجدد ربات" in audit_entry_ret.details

        # Check Admin Notification
        assert mock_notify_ret.called
        call_kwargs = mock_notify_ret.call_args.kwargs
        assert "استارت ربات توسط کاربر:" in call_kwargs["text"]
        assert "کاربر قبلی سامانه" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_start_flow_html_unescaped_name_edge_case(db_session: AsyncSession):
    """
    Edge case / flaw check:
    Verify whether special characters like '<', '>', '&' in user's first/last name
    are properly escaped or could break HTML formatting in admin alert.
    """
    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(
        telegram_id=444444,
        username="hacker_name",
        first_name="Hacker <script> & <test>",
        last_name="Smith",
    )
    await db_session.commit()

    mock_bot = AsyncMock()
    mock_message = AsyncMock()
    mock_message.chat.id = 444444
    mock_message.from_user.id = 444444
    mock_message.bot = mock_bot
    mock_command = MagicMock()
    mock_command.args = None
    mock_state = AsyncMock()

    with patch("app.bot.notifications.notify_all_admins", new_callable=AsyncMock) as mock_notify:
        await handle_start(
            message=mock_message,
            command=mock_command,
            db_user=user,
            db_session=db_session,
            bot=mock_bot,
            state=mock_state,
            is_new_user=True,
        )
        assert mock_notify.called
        sent_text = mock_notify.call_args.kwargs["text"]
        # In start.py lines 46-73, full_name is unescaped:
        # If unescaped, '<script>' will appear raw in HTML parse_mode, causing Telegram API rejection
        raw_unescaped = "<script>" in sent_text
        assert raw_unescaped, "Confirmed unescaped HTML tag in start.py admin alert message!"


# =========================================================================
# MISSION 2: Purchase Flow & Automatic Location Bypass
# =========================================================================

@pytest.fixture
async def setup_purchase_env(db_session: AsyncSession):
    """Setup products and servers for purchase flow tests."""
    panel = Panel(
        id=uuid.uuid4(),
        name="Test Panel 1",
        base_url="https://panel1.test:8443",
        panel_type="3x-ui",
        username="admin",
        _password_encrypted=encrypt_credential("admin"),
        is_active=True,
        is_healthy=True,
    )
    db_session.add(panel)

    server1 = Server(
        id=uuid.uuid4(),
        panel_id=panel.id,
        name="Server Germany",
        location_code="DE",
        location_name="آلمان",
        flag_emoji="🇩🇪",
        subscription_host="sub.de.vpnprivate.com",
        is_active=True,
        priority=100,
    )
    server2 = Server(
        id=uuid.uuid4(),
        panel_id=panel.id,
        name="Server Netherlands",
        location_code="NL",
        location_name="هلند",
        flag_emoji="🇳🇱",
        subscription_host="sub.nl.vpnprivate.com",
        is_active=True,
        priority=90,
    )
    db_session.add_all([server1, server2])

    # Case 1: Unlimited plan (traffic_bytes == 0)
    prod_unlimited = Product(
        id=uuid.uuid4(),
        name="Unlimited Multi-Location Plan",
        price_irr=500_000_0,
        traffic_bytes=0,
        duration_days=30,
        allowed_locations="ALL",
        is_active=True,
        sort_order=1,
    )

    # Case 2: Volume plan with single allowed location
    prod_single_loc = Product(
        id=uuid.uuid4(),
        name="Fixed Germany Plan",
        price_irr=300_000,
        traffic_bytes=30 * 1024 * 1024 * 1024,
        duration_days=30,
        allowed_locations="DE",
        is_active=True,
        sort_order=2,
    )

    # Case 3: Volume plan with multiple allowed locations
    prod_multi_loc = Product(
        id=uuid.uuid4(),
        name="Multi Location Volume Plan",
        price_irr=400_000_0,
        traffic_bytes=50 * 1024 * 1024 * 1024,
        duration_days=30,
        allowed_locations="ALL",
        is_active=True,
        sort_order=3,
    )

    db_session.add_all([prod_unlimited, prod_single_loc, prod_multi_loc])
    await db_session.commit()

    return {
        "panel": panel,
        "server1": server1,
        "server2": server2,
        "prod_unlimited": prod_unlimited,
        "prod_single_loc": prod_single_loc,
        "prod_multi_loc": prod_multi_loc,
    }


@pytest.mark.asyncio
async def test_purchase_case1_unlimited_plan_bypasses_location(db_session: AsyncSession, setup_purchase_env):
    """Case 1: Unlimited plan (traffic_bytes == 0) skips location selection -> entering_coupon."""
    env = setup_purchase_env
    prod = env["prod_unlimited"]

    callback = AsyncMock()
    callback.data = f"buy_prod:{prod.id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    state = AsyncMock()
    state_data = {}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_set_state(s):
        state_data["state"] = s

    state.update_data = mock_update_data
    state.set_state = mock_set_state

    await handle_product_selected(callback, state, db_session)

    assert state_data.get("product_id") == str(prod.id)
    assert state_data.get("location_code") == "ALL"
    assert state_data.get("state") == PurchaseOrderState.entering_coupon
    assert callback.message.edit_text.called
    edit_call = callback.message.edit_text.call_args
    assert "بسته نامحدود" in edit_call[0][0]
    # Check that skip_coupon button is present
    kb = edit_call[1]["reply_markup"]
    assert any(btn.callback_data == "skip_coupon" for row in kb.inline_keyboard for btn in row)


@pytest.mark.asyncio
async def test_purchase_case2_single_location_bypasses_location(db_session: AsyncSession, setup_purchase_env):
    """Case 2: Plan specifies single location in allowed_locations -> skips location selection, auto-assigns location."""
    env = setup_purchase_env
    prod = env["prod_single_loc"]

    callback = AsyncMock()
    callback.data = f"buy_prod:{prod.id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    state = AsyncMock()
    state_data = {}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_set_state(s):
        state_data["state"] = s

    state.update_data = mock_update_data
    state.set_state = mock_set_state

    await handle_product_selected(callback, state, db_session)

    assert state_data.get("product_id") == str(prod.id)
    assert state_data.get("location_code") == "DE"
    assert state_data.get("state") == PurchaseOrderState.entering_coupon
    assert callback.message.edit_text.called
    edit_call = callback.message.edit_text.call_args
    assert "انتخاب خودکار" in edit_call[0][0]
    # Check skip_coupon button
    kb = edit_call[1]["reply_markup"]
    assert any(btn.callback_data == "skip_coupon" for row in kb.inline_keyboard for btn in row)


@pytest.mark.asyncio
async def test_purchase_case2_single_server_available_bypasses_location(db_session: AsyncSession, setup_purchase_env):
    """Case 2b: Only 1 server available in system -> auto-assigns location, skips location selection."""
    env = setup_purchase_env
    # Deactivate server2
    env["server2"].is_active = False
    await db_session.commit()

    prod = env["prod_multi_loc"]  # allowed_locations is "ALL", but only 1 server active

    callback = AsyncMock()
    callback.data = f"buy_prod:{prod.id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    state = AsyncMock()
    state_data = {}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_set_state(s):
        state_data["state"] = s

    state.update_data = mock_update_data
    state.set_state = mock_set_state

    await handle_product_selected(callback, state, db_session)

    assert state_data.get("location_code") == "DE"
    assert state_data.get("state") == PurchaseOrderState.entering_coupon
    assert callback.message.edit_text.called
    assert "انتخاب خودکار" in callback.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_purchase_case3_multiple_locations_prompts_selection(db_session: AsyncSession, setup_purchase_env):
    """Case 3: Plan allows multiple locations -> prompts user with get_locations_keyboard."""
    env = setup_purchase_env
    env["server2"].is_active = True
    await db_session.commit()

    prod = env["prod_multi_loc"]

    callback = AsyncMock()
    callback.data = f"buy_prod:{prod.id}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    state = AsyncMock()
    state_data = {}

    async def mock_update_data(**kwargs):
        state_data.update(kwargs)

    async def mock_set_state(s):
        state_data["state"] = s

    state.update_data = mock_update_data
    state.set_state = mock_set_state

    await handle_product_selected(callback, state, db_session)

    assert state_data.get("product_id") == str(prod.id)
    assert state_data.get("state") == PurchaseOrderState.selecting_location
    assert callback.message.edit_text.called
    edit_call = callback.message.edit_text.call_args
    assert "موقعیت مکانی (لوکیشن) سرور مورد نظر خود را انتخاب نمایید" in edit_call[0][0]
    kb = edit_call[1]["reply_markup"]
    # Check server buttons
    button_datas = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "sel_loc:DE" in button_datas
    assert "sel_loc:NL" in button_datas
    assert "sel_loc:ALL" in button_datas


@pytest.mark.asyncio
async def test_coupon_validation_and_skip_action(db_session: AsyncSession, setup_purchase_env):
    """Test coupon validation (percentage, fixed amount, expired, invalid) and skip action."""
    env = setup_purchase_env
    prod = env["prod_single_loc"]

    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(telegram_id=555555, username="coupon_buyer")
    await db_session.commit()

    # Create coupons
    c_percent = Coupon(
        id=uuid.uuid4(),
        code="SAVE20",
        discount_percent=20,
        discount_amount_irr=0,
        max_uses=10,
        current_uses=0,
        is_active=True,
    )
    c_fixed = Coupon(
        id=uuid.uuid4(),
        code="OFF10K",
        discount_percent=0,
        discount_amount_irr=10_000_0,  # 10,000 Toman in Rials
        max_uses=5,
        current_uses=0,
        is_active=True,
    )
    c_expired = Coupon(
        id=uuid.uuid4(),
        code="EXPIRED",
        discount_percent=50,
        discount_amount_irr=0,
        max_uses=5,
        current_uses=0,
        is_active=True,
        valid_until=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add_all([c_percent, c_fixed, c_expired])
    await db_session.commit()

    order_svc = OrderService(db_session)

    # 1. Skip coupon
    order_skip = await order_svc.create_order(user_id=user.id, product_id=prod.id, coupon_code=None)
    assert order_skip.total_price_irr == prod.price_irr

    # 2. 20% Percent coupon
    order_perc = await order_svc.create_order(user_id=user.id, product_id=prod.id, coupon_code="SAVE20")
    expected_price_perc = prod.price_irr - (prod.price_irr * 20 // 100)
    assert order_perc.total_price_irr == expected_price_perc
    assert c_percent.current_uses == 1

    # 3. Fixed amount coupon
    order_fix = await order_svc.create_order(user_id=user.id, product_id=prod.id, coupon_code="off10k")
    assert order_fix.total_price_irr == prod.price_irr - 10_000_0
    assert c_fixed.current_uses == 1

    # 4. Expired coupon -> full price charged
    order_exp = await order_svc.create_order(user_id=user.id, product_id=prod.id, coupon_code="EXPIRED")
    assert order_exp.total_price_irr == prod.price_irr

    # 5. Invalid / non-existent coupon -> full price charged
    order_inv = await order_svc.create_order(user_id=user.id, product_id=prod.id, coupon_code="DOESNT_EXIST")
    assert order_inv.total_price_irr == prod.price_irr


@pytest.mark.asyncio
async def test_payment_methods_keyboard_generation():
    """Verify payment methods keyboard renders wallet, c2c, crypto, zarinpal correctly and callback <= 64 bytes."""
    order_id = str(uuid.uuid4())

    # Case A: can_wallet = True, all active
    active_provs = {
        "card_to_card": True,
        "crypto": True,
        "zarinpal": True,
        "sep": True,
        "idpay": True,
        "perfect_money": True,
        "stripe": True,
    }
    kb = get_payment_methods_keyboard(order_id, can_wallet=True, active_providers=active_provs)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    callbacks = [btn.callback_data for btn in buttons]

    assert any("pay_method:" in c and "WALLET" in c for c in callbacks)
    assert any("pay_method:" in c and "CARD_TO_CARD" in c for c in callbacks)
    assert any("pay_method:" in c and "CRYPTO" in c for c in callbacks)
    assert any("pay_method:" in c and "ZARINPAL" in c for c in callbacks)

    # Check 64-byte Telegram limit
    for btn in buttons:
        cb = btn.callback_data
        if cb:
            assert len(cb.encode("utf-8")) <= 64, f"Callback data exceeds 64 bytes: {cb}"

    # Case B: can_wallet = False -> Wallet button absent
    kb_no_wallet = get_payment_methods_keyboard(order_id, can_wallet=False, active_providers=active_provs)
    callbacks_no_w = [btn.callback_data for row in kb_no_wallet.inline_keyboard for btn in row]
    assert not any("WALLET" in c for c in callbacks_no_w)


# =========================================================================
# MISSION 3: Order Approval & Config Delivery
# =========================================================================

@pytest.mark.asyncio
async def test_order_approval_template_interpolation_and_safedict():
    """Verify safe interpolation for all variables in order_approved_delivery."""
    support_u = "VPNPrivateSupport"
    delivery_template = get_bot_text("order_approved_delivery")

    class SafeDict(dict):
        def __missing__(self, key):
            return f"{{{key}}}"

    conn = {
        "subscription_url": "https://sub.vpnprivate.com/sub/token123",
        "location": "آلمان",
        "flag": "🇩🇪",
        "protocol": "VLESS",
    }
    subscription_email = "user_12345@vpnprivate"

    customer_msg = delivery_template.format_map(
        SafeDict(
            client_email=subscription_email,
            location=conn.get("location", ""),
            flag=conn.get("flag", ""),
            protocol=conn.get("protocol", ""),
            subscription_url=conn.get("subscription_url", ""),
            support_username=support_u,
            remark=conn.get("remark", ""),
        )
    )

    assert subscription_email in customer_msg
    assert "https://sub.vpnprivate.com/sub/token123" in customer_msg
    assert "آلمان" in customer_msg
    assert "🇩🇪" in customer_msg
    assert "VLESS" in customer_msg

    # Test unknown extra placeholder does NOT cause KeyError
    custom_template = "Hello {client_email}, your plan is {custom_var}!"
    safe_formatted = custom_template.format_map(SafeDict(client_email="test@email"))
    assert safe_formatted == "Hello test@email, your plan is {custom_var}!"


@pytest.mark.asyncio
async def test_order_approval_remark_deficiency_bug(db_session: AsyncSession, setup_purchase_env):
    """
    Edge case check:
    Verify whether {remark} in get_connection_details is missing,
    causing {remark} in delivery message to always be an empty string.
    """
    env = setup_purchase_env
    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(telegram_id=666666, username="sub_user")

    inbound = Inbound(
        id=uuid.uuid4(),
        panel_id=env["panel"].id,
        remote_inbound_id=1,
        port=443,
        protocol="vless",
        remark="Test Inbound",
        stream_settings="{}",
        is_active=True,
    )
    db_session.add(inbound)
    await db_session.commit()

    sub = Subscription(
        id=uuid.uuid4(),
        user_id=user.id,
        server_id=env["server1"].id,
        panel_id=env["panel"].id,
        inbound_id=inbound.id,
        client_uuid=str(uuid.uuid4()),
        client_email="sub_user@vpnprivate",
        protocol="vless",
        subscription_token=secrets.token_hex(8),
        traffic_limit_bytes=10 * 1024 * 1024 * 1024,
        traffic_used_bytes=0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        status=SubscriptionStatus.ACTIVE,
    )
    db_session.add(sub)
    await db_session.commit()

    sub_svc = SubscriptionService(db_session)
    conn = await sub_svc.get_connection_details(sub.id)

    # Check whether 'remark' is in conn
    assert "remark" not in conn, (
        "BUG CONFIRMED: SubscriptionService.get_connection_details does not include 'remark' in its return dict, "
        "causing conn.get('remark', '') to always evaluate to empty string in delivery messages!"
    )


@pytest.mark.asyncio
async def test_order_approval_qr_buffer():
    """Verify QR code generator produces valid PNG stream."""
    url = "https://sub.vpnprivate.com/sub/abcdef1234567890"
    qr_buf = generate_qr_buffer(url)
    assert isinstance(qr_buf, io.BytesIO)
    data = qr_buf.getvalue()
    assert len(data) > 0
    # PNG signature check: \x89PNG\r\n\x1a\n
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


# =========================================================================
# MISSION 4: Voucher & Gift Card Flow
# =========================================================================

@pytest.mark.asyncio
async def test_voucher_wallet_credit_redemption(db_session: AsyncSession):
    """Verify voucher redemption for wallet credit, audit log creation, and admin alert."""
    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(telegram_id=777777, username="voucher_user")
    await db_session.commit()

    voucher_svc = VoucherService(db_session)
    batch_id, vouchers = await voucher_svc.generate_batch(
        count=1,
        voucher_type="wallet_credit",
        amount_toman=50_000,
    )
    v = vouchers[0]
    await db_session.commit()

    # 1. Invalid code test
    res_inv = await voucher_svc.redeem_voucher("NON_EXISTENT_CODE", user)
    assert res_inv["success"] is False
    assert res_inv["error"] == "NOT_FOUND"

    # 2. Redeem valid voucher
    res_val = await voucher_svc.redeem_voucher(v.code, user)
    assert res_val["success"] is True
    assert res_val["type"] == "wallet_credit"
    assert res_val["amount_toman"] == 50_000
    assert res_val["new_balance_toman"] == 50_000

    # Verify voucher marked used in DB
    refreshed_v = await db_session.get(Voucher, v.id)
    assert refreshed_v.is_used is True
    assert refreshed_v.used_by_telegram_id == 777777
    assert refreshed_v.used_at is not None

    # Verify user wallet balance updated in DB
    w_res = await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))
    wallet = w_res.scalar_one_or_none()
    assert wallet.balance_toman == 50_000

    # 3. Already used code test
    res_used = await voucher_svc.redeem_voucher(v.code, user)
    assert res_used["success"] is False
    assert res_used["error"] == "ALREADY_USED"


@pytest.mark.asyncio
async def test_voucher_process_redemption_bot_handler_audit_and_alert(db_session: AsyncSession):
    """Test process_voucher_redemption handler writes AuditLog and calls notify_all_admins."""
    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(telegram_id=888888, username="voucher_bot_user")
    await db_session.commit()

    voucher_svc = VoucherService(db_session)
    _, vouchers = await voucher_svc.generate_batch(
        count=1,
        voucher_type="wallet_credit",
        amount_toman=25_000,
    )
    v = vouchers[0]
    await db_session.commit()

    mock_msg = AsyncMock()
    mock_msg.chat.id = 888888
    mock_msg.answer = AsyncMock()
    mock_bot = AsyncMock()

    with patch("app.bot.notifications.notify_all_admins", new_callable=AsyncMock) as mock_notify:
        await process_voucher_redemption(
            target_message=mock_msg,
            user=user,
            code=v.code,
            db_session=db_session,
            bot=mock_bot,
        )
        await db_session.commit()

        # Check Audit Log
        audit_res = await db_session.execute(
            select(AuditLog).where(AuditLog.actor_id == 888888, AuditLog.action == "VOUCHER_REDEEMED")
        )
        audit_entry = audit_res.scalar_one_or_none()
        assert audit_entry is not None
        assert "مصرف ووچر کارت" in audit_entry.details

        # Check Admin Alert
        assert mock_notify.called
        call_text = mock_notify.call_args.kwargs["text"]
        assert "ووچر کد در ربات مصرف شد!" in call_text
        assert v.code in call_text
        assert "25,000 تومان" in call_text


@pytest.mark.asyncio
async def test_gift_card_creation_and_redemption_flow(db_session: AsyncSession, setup_purchase_env):
    """Test GiftCardService creation, balance deduction, and deep-link redemption."""
    env = setup_purchase_env
    prod = env["prod_single_loc"]

    user_service = UserService(db_session)
    buyer, _ = await user_service.register_or_get_user(telegram_id=999991, username="gift_buyer")
    recipient, _ = await user_service.register_or_get_user(telegram_id=999992, username="gift_recipient")
    await db_session.commit()

    # Credit buyer wallet with 500,000 Toman (5,000,000 Rials)
    wallet_svc = WalletService(db_session)
    await wallet_svc.credit(buyer.id, 500_000 * 10, "TEST_DEPOSIT", "Gift Card Test")
    await db_session.commit()

    # 1. Buyer creates gift card
    gift_res = await GiftCardService.create_gift_card(
        user=buyer,
        product_id=prod.id,
        sender_name="Best Friend",
        gift_note="Happy Birthday!",
        db=db_session,
    )
    assert gift_res["success"] is True
    assert gift_res["code"].startswith("GIFT-")
    assert gift_res["new_balance_toman"] == 500_000 - prod.price_toman

    # 2. Check gift info
    info = await GiftCardService.get_gift_info(gift_res["code"], db_session)
    assert info["exists"] is True
    assert info["is_used"] is False
    assert info["sender_name"] == "Best Friend"
    assert info["gift_note"] == "Happy Birthday!"


# =========================================================================
# MISSION 5: Free Trial Account Flow
# =========================================================================

@pytest.mark.asyncio
async def test_trial_email_generation_with_effective_email_suffix(db_session: AsyncSession):
    """Verify trial email uses SettingsService.get_effective_email_suffix."""
    with patch.object(SettingsService, "get_value", return_value="@testdomain.com"):
        suffix = await SettingsService.get_effective_email_suffix(db_session)
        assert suffix == "@testdomain.com"


@pytest.mark.asyncio
async def test_free_trial_limits_and_abuse_prevention(db_session: AsyncSession, setup_purchase_env):
    """Verify free trial limits, enforcement of max trials, and user row locking."""
    env = setup_purchase_env
    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(telegram_id=999001, username="trial_hunter")

    inbound = Inbound(
        id=uuid.uuid4(),
        panel_id=env["panel"].id,
        remote_inbound_id=1,
        port=443,
        protocol="vless",
        remark="Trial Inbound",
        stream_settings="{}",
        is_active=True,
    )
    db_session.add(inbound)
    await db_session.commit()

    mock_msg = AsyncMock()
    mock_msg.chat.id = 999001
    mock_msg.answer = AsyncMock(return_value=AsyncMock())
    mock_msg.answer_photo = AsyncMock()

    # Case A: Trial is disabled
    orig_trial_enabled = getattr(settings, "TRIAL_ENABLED", True)
    try:
        settings.TRIAL_ENABLED = False
        await handle_free_trial(mock_msg, user, db_session)
        assert mock_msg.answer.called
        call_text = mock_msg.answer.call_args[0][0]
        assert "دریافت اکانت تست موقتاً توسط مدیریت غیرفعال شده است" in call_text
    finally:
        settings.TRIAL_ENABLED = orig_trial_enabled

    # Case B: First Trial provisioning
    orig_max_count = getattr(settings, "TRIAL_MAX_COUNT", 1)
    try:
        settings.TRIAL_ENABLED = True
        settings.TRIAL_MAX_COUNT = 1
        with patch("app.modules.subscriptions.service.SubscriptionService._get_panel_adapter") as mock_adapter_getter:
            mock_adapter = AsyncMock()
            mock_adapter.get_subscription_url = AsyncMock(return_value="https://sub.test/sub/trial123")
            mock_adapter.generate_client_url = MagicMock(return_value="vless://trial123@sub.test:443")
            mock_adapter_getter.return_value = mock_adapter

            await handle_free_trial(mock_msg, user, db_session)
            await db_session.commit()

            # Verify trial subscription created
            trial_subs = (await db_session.execute(
                select(Subscription).where(
                    Subscription.user_id == user.id,
                    Subscription.client_email.like("trial_%"),
                )
            )).scalars().all()
            assert len(trial_subs) == 1
            created_sub = trial_subs[0]
            assert created_sub.client_email.startswith(f"trial_999001_")

            # Verify Audit Log
            audit_res = await db_session.execute(
                select(AuditLog).where(AuditLog.actor_id == 999001, AuditLog.action == "TRIAL_ACTIVATED")
            )
            assert audit_res.scalar_one_or_none() is not None

        # Case C: Second Trial attempt when TRIAL_MAX_COUNT = 1 -> BLOCKED
        mock_msg_2 = AsyncMock()
        mock_msg_2.chat.id = 999001
        mock_msg_2.answer = AsyncMock()

        await handle_free_trial(mock_msg_2, user, db_session)
        assert mock_msg_2.answer.called
        call_text_2 = mock_msg_2.answer.call_args[0][0]
        assert "قبلاً سقف مجاز اکانت تست رایگان خود" in call_text_2
    finally:
        settings.TRIAL_ENABLED = orig_trial_enabled
        settings.TRIAL_MAX_COUNT = orig_max_count


@pytest.mark.asyncio
async def test_voucher_plan_subscription_redemption(db_session: AsyncSession, setup_purchase_env):
    """Verify plan voucher redemption grants exact days (e.g., 365 days for 12 months) and creates order."""
    env = setup_purchase_env
    prod = env["prod_single_loc"]

    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(telegram_id=888111, username="voucher_plan_user")
    await db_session.commit()

    voucher_svc = VoucherService(db_session)
    # Generate 12-month voucher
    code = generate_voucher_code()
    voucher = Voucher(
        id=uuid.uuid4(),
        code=code,
        voucher_type="plan",
        duration_months=12,
        duration_days=365,
        product_id=prod.id,
        is_used=False,
    )
    db_session.add(voucher)
    await db_session.commit()

    # Mock adapter provisioning
    with patch("app.modules.subscriptions.service.SubscriptionService._get_panel_adapter") as mock_adapter_getter:
        mock_adapter = AsyncMock()
        mock_adapter.get_subscription_url = AsyncMock(return_value="https://sub.test/sub/voucher_sub")
        mock_adapter.generate_client_url = MagicMock(return_value="vless://voucher_sub@sub.test:443")
        mock_adapter_getter.return_value = mock_adapter

        res = await voucher_svc.redeem_voucher(code, user)
        assert res["success"] is True
        assert res["type"] == "plan"
        assert res["subscription"] is not None
        assert "365 روز" in res["message"]
        assert "12 ماه کامل" in res["message"]

        # Check voucher marked used in DB
        v_db = await db_session.get(Voucher, voucher.id)
        assert v_db.is_used is True
        assert v_db.used_by_telegram_id == 888111


@pytest.mark.asyncio
async def test_admin_bot_approve_payment_flow(db_session: AsyncSession, setup_purchase_env):
    """Verify approve_payment_by_admin handler delivers subscription and QR to customer and notifies other admins."""
    env = setup_purchase_env
    user_service = UserService(db_session)
    admin_user, _ = await user_service.register_or_get_user(telegram_id=1001, username="admin_boss")
    admin_user.role = UserRole.ADMIN
    customer, _ = await user_service.register_or_get_user(telegram_id=2002, username="customer_buyer")
    await db_session.commit()

    # Create order & payment
    order_svc = OrderService(db_session)
    order = await order_svc.create_order(user_id=customer.id, product_id=env["prod_single_loc"].id)
    payment, _ = await order_svc.initiate_payment(order.id, PaymentMethod.CARD_TO_CARD)
    await db_session.commit()

    callback = AsyncMock()
    callback.data = f"admin_pay_approve:{payment.id}"
    callback.from_user.id = 1001
    callback.from_user.first_name = "BossAdmin"
    callback.from_user.username = "admin_boss"
    callback.message = AsyncMock()
    callback.message.caption = "Payment receipt caption"
    callback.message.text = None

    mock_bot = AsyncMock()
    mock_bot.send_photo = AsyncMock()
    mock_bot.send_message = AsyncMock()

    with patch("app.modules.subscriptions.service.SubscriptionService._get_panel_adapter") as mock_adapter_getter, \
         patch("app.bot.notifications.notify_all_admins", new_callable=AsyncMock) as mock_notify:
        mock_adapter = AsyncMock()
        mock_adapter.get_subscription_url = AsyncMock(return_value="https://sub.test/sub/approved123")
        mock_adapter.generate_client_url = MagicMock(return_value="vless://approved123@sub.test:443")
        mock_adapter_getter.return_value = mock_adapter

        await approve_payment_by_admin(
            callback=callback,
            bot=mock_bot,
            db_user=admin_user,
            db_session=db_session,
        )
        await db_session.commit()

        # Check that subscription was delivered to customer via send_photo
        assert mock_bot.send_photo.called
        call_kwargs = mock_bot.send_photo.call_args.kwargs
        assert call_kwargs["chat_id"] == 2002
        assert "پرداخت شما تایید شد" in call_kwargs["caption"]
        assert "sub.test/sub/approved123" in call_kwargs["caption"]

        # Check broadcast to other admins
        assert mock_notify.called
        notify_call = mock_notify.call_args.kwargs
        assert "تایید سفارش و فعال‌سازی" in notify_call["text"]
        assert notify_call["exclude_telegram_id"] == 1001


@pytest.mark.asyncio
async def test_trial_abuse_bypass_if_sub_deleted_flaw(db_session: AsyncSession, setup_purchase_env):
    """
    Design flaw check:
    handle_free_trial checks trial limit using `select(count) from subscriptions where client_email like 'trial_%'`.
    If an expired trial subscription is pruned/deleted, user_trial_count resets to 0,
    allowing customer to abuse free trials indefinitely despite AuditLog having records.
    """
    env = setup_purchase_env
    user_service = UserService(db_session)
    user, _ = await user_service.register_or_get_user(telegram_id=999888, username="clever_trial_user")

    # Manually create a previous trial record in AuditLog
    audit_entry = AuditLog(
        actor_id=999888,
        actor_role="CUSTOMER",
        action="TRIAL_ACTIVATED",
        target_type="Subscription",
        target_id=str(uuid.uuid4()),
        details="Previous trial that expired and was deleted",
    )
    db_session.add(audit_entry)
    await db_session.commit()

    # Query using AuditLog (historical truth)
    audit_trials_count = (await db_session.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.actor_id == 999888,
            AuditLog.action == "TRIAL_ACTIVATED",
        )
    )).scalar() or 0
    assert audit_trials_count == 1

    # Query using current logic in start.py line 360:
    current_check_count = (await db_session.execute(
        select(func.count(Subscription.id)).where(
            Subscription.user_id == user.id,
            Subscription.client_email.like("trial_%"),
        )
    )).scalar() or 0

    # Because subscription was deleted/pruned, current check returns 0!
    assert current_check_count == 0, (
        "CONFIRMED FLAW: Trial limit check queries active/existing Subscription rows instead of AuditLog. "
        "When expired trials are pruned, user can claim unlimited free trials!"
    )

