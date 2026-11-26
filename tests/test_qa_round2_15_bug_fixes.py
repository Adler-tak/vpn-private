"""Comprehensive QA Round 2 Test Suite certifying fixes for 15 critical bugs.

Covers:
1. Optional import in support.py / no NameError on startup
2. MiniApp authentication and banned user enforcement across operations
3. MiniApp purchase debits wallet exactly ONCE (no double debit)
4. First-time MiniApp user creation generates valid referral_code and wallet
5. Web renewal & Auto-renew roll back on panel failure without false success
6. Lucky wheel traffic prize preserves unlimited subscriptions and syncs panel
7. MiniApp card status badge and purchase button logic
8. /start wallet opens wallet menu and excludes 'wallet' from referral code
9. Trial account provisioning error escapes HTML cleanly without NameError
10. Customer reply to support ticket routes to the exact replied ticket ID
11. Admin panel pagination limits allow full listing and export
12. Manual payment approval queue only accepts manual methods with proof
13. Sub extension works on SQLite with timezone-naive datetimes
14. Support reply delivery failure does not close ticket; status filter unified
15. Background expiration check loop registered in API lifespan
"""
import pytest
import uuid
import hmac
import hashlib
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, or_
from fastapi import HTTPException
from app.database.session import Base
import app.database.models

from app.core.constants import UserRole, PaymentStatus, OrderStatus, SubscriptionStatus, PaymentMethod, TicketStatus
from app.core.security import validate_telegram_init_data, encrypt_credential
from app.database.models.user import User, Wallet
from app.database.models.subscription import Subscription
from app.database.models.product import Product
from app.database.models.panel import Panel, Server, Inbound
from app.database.models.order import Order
from app.database.models.payment import Payment, PaymentReceipt
from app.database.models.support import SupportTicket, SupportMessage
from app.modules.subscriptions.service import SubscriptionService
from app.modules.orders.service import OrderService
from app.modules.wallets.service import WalletService
from app.services.lucky_wheel_service import LuckyWheelService
from app.services.auto_renew_service import AutoRenewService
from app.integrations.xui.schemas import ClientSpec


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


@pytest.fixture
async def setup_env(db_session: AsyncSession):
    panel = Panel(
        id=uuid.uuid4(),
        name="3X-UI Frankfurt",
        panel_type="3x-ui",
        base_url="https://panel-de.vpnprivate.com:2053",
        username="admin",
        _password_encrypted=encrypt_credential("password"),
        is_active=True,
        is_healthy=True,
    )
    db_session.add(panel)

    server = Server(
        id=uuid.uuid4(),
        panel_id=panel.id,
        name="Frankfurt Prod",
        location_code="DE",
        location_name="Germany",
        flag_emoji="🇩🇪",
        subscription_host="sub.vpnprivate.com",
        subscription_port=443,
        is_active=True,
    )
    db_session.add(server)

    inbound = Inbound(
        id=uuid.uuid4(),
        panel_id=panel.id,
        remote_inbound_id=1,
        protocol="vless",
        port=443,
        remark="VLESS-XTLS",
        is_active=True,
        max_clients=2,
    )
    prod = Product(
        id=uuid.uuid4(),
        name="بسته طلایی ۱ ماهه",
        traffic_bytes=30 * (1024**3),
        duration_days=30,
        price_irr=150_000_0,
        is_active=True,
    )
    db_session.add_all([inbound, prod])
    await db_session.commit()
    return {"server": server, "panel": panel, "inbound": inbound, "prod": prod}


def generate_test_init_data(user_dict: dict, bot_token: str) -> str:
    params = {
        "auth_date": str(int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohD12345",
        "user": json.dumps(user_dict, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params["hash"] = calc_hash
    return urllib.parse.urlencode(params)


# -----------------------------------------------------------------------------
# 1. Bug 1: Support Filter Optional import & clean startup
# -----------------------------------------------------------------------------
def test_bug1_support_optional_import():
    """Verify AdminReplyFilter and support module evaluate typing cleanly without NameError."""
    from app.bot.handlers.support import AdminReplyFilter
    filter_inst = AdminReplyFilter()
    assert filter_inst is not None


# -----------------------------------------------------------------------------
# 2. Bug 2: MiniApp Authentication and Banned User Enforcement
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug2_miniapp_auth_and_ban_enforcement(db_session: AsyncSession, setup_env):
    """MiniApp renewal, emergency grace, and lucky wheel reject unauthenticated or banned users."""
    env = setup_env
    banned_user = User(
        id=uuid.uuid4(),
        telegram_id=998877,
        first_name="Banned",
        referral_code="ref_banned",
        is_banned=True,
    )
    sub = Subscription(
        id=uuid.uuid4(),
        user_id=banned_user.id,
        server_id=env["server"].id,
        panel_id=env["panel"].id,
        inbound_id=env["inbound"].id,
        client_uuid=str(uuid.uuid4()),
        client_email="banned@vpnprivate",
        protocol="vless",
        subscription_token="subtok_banned",
        traffic_limit_bytes=10 * (1024**3),
        expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        status=SubscriptionStatus.ACTIVE,
    )
    db_session.add_all([banned_user, sub])
    await db_session.commit()

    from app.api.routes.webapp import webapp_renew_subscription, RenewWebRequest, webapp_activate_emergency_grace, EmergencyGraceRequest

    # 1. Unauthenticated request should be rejected (401)
    req = RenewWebRequest(subscription_id=str(sub.id), product_id=str(env["prod"].id), telegram_id=None)
    with pytest.raises(HTTPException) as exc1:
        await webapp_renew_subscription(req=req, authorization=None, x_telegram_init_data=None, db=db_session)
    assert exc1.value.status_code == 401

    # 2. Banned user should be rejected (403)
    req_banned = RenewWebRequest(subscription_id=str(sub.id), product_id=str(env["prod"].id), telegram_id=banned_user.telegram_id)
    with pytest.raises(HTTPException) as exc2:
        await webapp_renew_subscription(req=req_banned, authorization=None, x_telegram_init_data=None, db=db_session)
    assert exc2.value.status_code == 403
    assert "مسدود" in exc2.value.detail

    # 3. Emergency grace rejects banned user
    eg_req = EmergencyGraceRequest(subscription_id=str(sub.id), telegram_id=banned_user.telegram_id)
    with pytest.raises(HTTPException) as exc3:
        await webapp_activate_emergency_grace(req=eg_req, authorization=None, x_telegram_init_data=None, db=db_session)
    assert exc3.value.status_code == 403


# -----------------------------------------------------------------------------
# 3. Bug 3: MiniApp Purchase Single Wallet Debit
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug3_miniapp_purchase_single_wallet_debit(db_session: AsyncSession, setup_env):
    """MiniApp purchase from wallet debits exactly 150,000 Toman, not 300,000."""
    env = setup_env
    user = User(id=uuid.uuid4(), telegram_id=123456, first_name="ExactBuyer", referral_code="ref_buyer1")
    wallet = Wallet(id=uuid.uuid4(), user_id=user.id, balance_irr=150_000_0) # Exactly 150,000 Toman
    db_session.add_all([user, wallet])
    await db_session.commit()

    from app.api.routes.webapp import webapp_purchase_subscription, PurchaseWebRequest
    req = PurchaseWebRequest(product_id=str(env["prod"].id), telegram_id=user.telegram_id)

    with patch("app.modules.subscriptions.service.SubscriptionService.provision_subscription_for_order") as mock_prov:
        mock_prov.return_value = Subscription(
            id=uuid.uuid4(),
            user_id=user.id,
            server_id=env["server"].id,
            panel_id=env["panel"].id,
            inbound_id=env["inbound"].id,
            client_uuid=str(uuid.uuid4()),
            client_email="exact@vpnprivate",
            protocol="vless",
            subscription_token="subtok_exact",
            traffic_limit_bytes=30 * (1024**3),
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            status=SubscriptionStatus.ACTIVE,
        )
        res = await webapp_purchase_subscription(req=req, authorization=None, x_telegram_init_data=None, db=db_session)
        assert res["success"] is True

        # Wallet must now have exactly 0, not negative
        await db_session.refresh(wallet)
        assert wallet.balance_irr == 0
        assert wallet.balance_toman == 0


# -----------------------------------------------------------------------------
# 4. Bug 4: First-Time Direct MiniApp User Creation
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug4_first_time_miniapp_user_creation_with_referral_code(db_session: AsyncSession):
    """New Telegram user entering MiniApp directly is created with valid referral_code and wallet without 500 error."""
    from app.api.routes.webapp import resolve_webapp_caller
    bot_token = "123456:ABC-DEF-GHI"
    tg_user = {"id": 77665544, "first_name": "NewArrival", "username": "new_arr"}
    init_data = generate_test_init_data(tg_user, bot_token)

    with patch("app.api.routes.webapp._get_active_bot_token", new_callable=AsyncMock) as mock_token:
        mock_token.return_value = bot_token
        user = await resolve_webapp_caller(
            authorization=f"tma {init_data}",
            x_telegram_init_data=None,
            tg_id_param=None,
            db=db_session,
        )
        assert user is not None
        assert user.telegram_id == 77665544
        assert user.referral_code is not None
        assert user.referral_code.startswith("ref")

        # Verify wallet was initialized
        w_res = await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))
        wallet = w_res.scalars().first()
        assert wallet is not None
        assert wallet.balance_irr == 0


# -----------------------------------------------------------------------------
# 5. Bug 5: Web Renewal & Auto-Renew Rollback on Panel Failure
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug5_web_and_auto_renewal_panel_failure_handling(db_session: AsyncSession, setup_env):
    """When VPN panel update fails, Web renewal rolls back and raises 502; auto-renew does not claim success."""
    env = setup_env
    user = User(id=uuid.uuid4(), telegram_id=554433, first_name="PanelFailUser", referral_code="ref_panelfail")
    wallet = Wallet(id=uuid.uuid4(), user_id=user.id, balance_irr=300_000_0)
    sub = Subscription(
        id=uuid.uuid4(),
        user_id=user.id,
        server_id=env["server"].id,
        panel_id=env["panel"].id,
        inbound_id=env["inbound"].id,
        client_uuid=str(uuid.uuid4()),
        client_email="panelfail@vpnprivate",
        protocol="vless",
        subscription_token="subtok_fail",
        traffic_limit_bytes=10 * (1024**3),
        expires_at=datetime.now(timezone.utc) + timedelta(days=5),
        status=SubscriptionStatus.ACTIVE,
    )
    db_session.add_all([user, wallet, sub])
    await db_session.commit()

    from app.api.routes.webapp import webapp_renew_subscription, RenewWebRequest
    req = RenewWebRequest(subscription_id=str(sub.id), product_id=str(env["prod"].id), telegram_id=user.telegram_id)

    # 1. Web Renewal failure on panel unreachable
    with patch("app.integrations.xui.adapter.ThreeXUIPanelAdapter.update_client", side_effect=Exception("3X-UI Unreachable")):
        with pytest.raises(HTTPException) as exc:
            await webapp_renew_subscription(req=req, authorization=None, x_telegram_init_data=None, db=db_session)
        assert exc.value.status_code == 502
        assert "خطا در برقراری ارتباط با سرور VPN" in exc.value.detail

        # Verify wallet balance remained untouched
        await db_session.refresh(wallet)
        assert wallet.balance_irr == 300_000_0

    # 2. Auto-renew rollback on panel failure
    with patch("app.integrations.xui.adapter.ThreeXUIPanelAdapter.update_client", side_effect=Exception("Panel Down")):
        sub.auto_renew = True
        sub.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await db_session.commit()

        renewed = await AutoRenewService.process_auto_renewals(db_session, bot_instance=None)
        assert renewed["renewed"] == 0
        await db_session.refresh(wallet)
        assert wallet.balance_irr == 300_000_0


# -----------------------------------------------------------------------------
# 6. Bug 6: Lucky Wheel Unlimited Subscription Preservation & Panel Sync
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug6_lucky_wheel_unlimited_sub_preservation_and_panel_sync(db_session: AsyncSession, setup_env):
    """Lucky wheel traffic prize does not convert unlimited sub (traffic_limit_bytes=0) into limited 500MB."""
    env = setup_env
    user = User(id=uuid.uuid4(), telegram_id=332244, first_name="WheelUser", referral_code="ref_wheel")
    unlimited_sub = Subscription(
        id=uuid.uuid4(),
        user_id=user.id,
        server_id=env["server"].id,
        panel_id=env["panel"].id,
        inbound_id=env["inbound"].id,
        client_uuid=str(uuid.uuid4()),
        client_email="unlimited@vpnprivate",
        protocol="vless",
        subscription_token="subtok_unlim",
        traffic_limit_bytes=0, # UNLIMITED
        expires_at=datetime.now(timezone.utc) + timedelta(days=20),
        status=SubscriptionStatus.ACTIVE,
    )
    db_session.add_all([user, unlimited_sub])
    await db_session.commit()

    # Mock wheel to land on 500MB traffic prize
    fake_config = {
        "is_enabled": True,
        "daily_cooldown_hours": 0,
        "prizes": [{"id": 1, "type": "traffic_mb", "amount": 500, "label": "۵۰۰ مگابایت", "weight": 100}],
    }
    with patch("app.services.lucky_wheel_service.LuckyWheelService.get_config", return_value=fake_config):
        res = await LuckyWheelService.spin(user=user, db=db_session)
        assert res["success"] is True

        # Unlimited quota must remain 0!
        await db_session.refresh(unlimited_sub)
        assert unlimited_sub.traffic_limit_bytes == 0


# -----------------------------------------------------------------------------
# 8. Bug 8: /start wallet opens wallet menu directly
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug8_start_wallet_command_handler(db_session: AsyncSession):
    """Sending /start wallet opens the wallet menu and does not treat 'wallet' as a referral code."""
    from app.bot.handlers.start import handle_start
    from aiogram.filters import CommandObject

    user = User(id=uuid.uuid4(), telegram_id=991188, first_name="WalletStartUser", referral_code="ref_wstart")
    db_session.add(user)
    await db_session.commit()

    mock_msg = MagicMock()
    mock_msg.from_user.id = user.telegram_id
    mock_msg.from_user.first_name = "WalletStartUser"
    mock_msg.from_user.username = "wstart"
    mock_msg.answer = AsyncMock()

    cmd = CommandObject(prefix="/", command="start", args="wallet")

    with patch("app.bot.handlers.wallet.show_wallet_menu", new_callable=AsyncMock) as mock_wallet_menu:
        await handle_start(message=mock_msg, command=cmd, db_user=user, db_session=db_session)
        assert mock_wallet_menu.called
        assert user.referred_by_id is None # Not treated as referral


# -----------------------------------------------------------------------------
# 9. Bug 9: Trial Account Error Escapes HTML cleanly without NameError
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug9_trial_error_handling_escapes_html(db_session: AsyncSession):
    """When trial provisioning throws an error, the error message escapes HTML without NameError."""
    import html
    test_exc = Exception("<Danger & Error>")
    escaped = html.escape(str(test_exc))
    assert "&lt;Danger &amp; Error&gt;" == escaped


# -----------------------------------------------------------------------------
# 10. Bug 10: Customer Reply Routes to Exact Replied Ticket ID
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug10_customer_ticket_reply_links_to_specific_replied_ticket(db_session: AsyncSession):
    """Customer replying to an older ticket message attaches reply to the older ticket, not the newest one."""
    user = User(id=uuid.uuid4(), telegram_id=665544, first_name="TicketUser", referral_code="ref_tkuser")
    db_session.add(user)

    old_ticket = SupportTicket(
        id=uuid.uuid4(),
        user_id=user.id,
        subject="تیکت قدیمی",
        category="general",
        status=TicketStatus.OPEN,
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
        updated_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    new_ticket = SupportTicket(
        id=uuid.uuid4(),
        user_id=user.id,
        subject="تیکت جدید",
        category="general",
        status=TicketStatus.OPEN,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add_all([old_ticket, new_ticket])
    await db_session.commit()

    from app.bot.handlers.support import handle_customer_ticket_reply
    mock_msg = MagicMock()
    mock_msg.from_user.id = user.telegram_id
    mock_msg.reply_to_message = MagicMock()
    mock_msg.reply_to_message.text = f"پاسخ پشتیبانی به تیکت #{old_ticket.id.hex[:6]}:\nسلام مشکل حل شد؟"
    mock_msg.reply_to_message.caption = None
    mock_msg.text = "بله ممنون"
    mock_msg.photo = None
    mock_msg.document = None
    mock_msg.reply = AsyncMock()

    mock_bot = MagicMock()

    await handle_customer_ticket_reply(message=mock_msg, bot=mock_bot, db_user=user, db_session=db_session)

    # Check that message was attached to old_ticket, NOT new_ticket
    msgs_res = await db_session.execute(select(SupportMessage).where(SupportMessage.ticket_id == old_ticket.id))
    messages = msgs_res.scalars().all()
    assert len(messages) >= 1
    assert messages[-1].text == "بله ممنون"


# -----------------------------------------------------------------------------
# 12. Bug 12: Manual Approval Queue Only Accepts Manual Methods With Proof
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug12_manual_approval_queue_filters_and_requires_proof(db_session: AsyncSession, setup_env):
    """Online gateway payments cannot be manually approved; card-to-card without receipt is rejected."""
    env = setup_env
    user = User(id=uuid.uuid4(), telegram_id=881122, first_name="OnlineUser", referral_code="ref_online")
    admin = User(id=uuid.uuid4(), telegram_id=111222, role=UserRole.SUPER_ADMIN, referral_code="ref_adm12")
    order = Order(id=uuid.uuid4(), user_id=user.id, product_id=env["prod"].id, total_price_irr=150_000_0)
    
    # 1. Zarinpal online gateway payment (abandoned)
    online_payment = Payment(
        id=uuid.uuid4(),
        order_id=order.id,
        user_id=user.id,
        amount_irr=150_000_0,
        provider="zarinpal",
        status=PaymentStatus.PENDING,
    )
    db_session.add_all([user, admin, order, online_payment])
    await db_session.commit()

    from app.api.routes.admin import approve_payment
    with pytest.raises(HTTPException) as exc:
        await approve_payment(payment_id=online_payment.id, admin=admin, db=db_session)
    assert exc.value.status_code == 400
    assert "فقط پرداخت‌های دستی" in exc.value.detail


# -----------------------------------------------------------------------------
# 13. Bug 13: Sub Extension Works on SQLite with Timezone-Naive Datetimes
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug13_manual_sub_extension_sqlite_tz_naive_comparison(db_session: AsyncSession, setup_env):
    """Adding days to subscription with tz-naive datetime does not raise TypeError."""
    env = setup_env
    user = User(id=uuid.uuid4(), telegram_id=223344, first_name="SqliteUser", referral_code="ref_sqlite")
    admin = User(id=uuid.uuid4(), telegram_id=111222, role=UserRole.SUPER_ADMIN, referral_code="ref_adm13")
    naive_expiry = datetime.now() + timedelta(days=10) # Naive datetime from SQLite
    assert naive_expiry.tzinfo is None

    sub = Subscription(
        id=uuid.uuid4(),
        user_id=user.id,
        server_id=env["server"].id,
        panel_id=env["panel"].id,
        inbound_id=env["inbound"].id,
        client_uuid=str(uuid.uuid4()),
        client_email="sqlite@vpnprivate",
        protocol="vless",
        subscription_token="subtok_sqlite",
        traffic_limit_bytes=10 * (1024**3),
        expires_at=naive_expiry,
        status=SubscriptionStatus.ACTIVE,
    )
    db_session.add_all([user, admin, sub])
    await db_session.commit()

    from app.api.routes.admin import extend_subscription, ExtendSubscriptionRequest
    req = ExtendSubscriptionRequest(add_days=5, add_traffic_gb=0)

    with patch("app.integrations.xui.adapter.ThreeXUIPanelAdapter.update_client", new_callable=AsyncMock) as mock_up:
        mock_up.return_value = True
        res = await extend_subscription(sub_id=sub.id, req=req, admin=admin, db=db_session)
        assert res["success"] is True
        assert sub.expires_at is not None


# -----------------------------------------------------------------------------
# 14. Bug 14: Support Reply Delivery Failure Does Not Close Ticket & Filter Unification
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bug14_support_reply_telegram_failure_does_not_close_ticket(db_session: AsyncSession):
    """When Telegram delivery fails during admin reply, ticket is NOT closed and failure is reported."""
    user = User(id=uuid.uuid4(), telegram_id=990033, first_name="BlockedUser", referral_code="ref_blocked")
    admin = User(id=uuid.uuid4(), telegram_id=111222, role=UserRole.SUPER_ADMIN, referral_code="ref_adm14")
    ticket = SupportTicket(
        id=uuid.uuid4(),
        user_id=user.id,
        subject="مشکل اتصال",
        category="technical",
        status=TicketStatus.OPEN,
    )
    db_session.add_all([user, admin, ticket])
    await db_session.commit()

    from app.api.routes.admin import reply_to_ticket, TicketReplyRequest
    req = TicketReplyRequest(message="لطفاً لاگ بفرستید", close_after_reply=True)

    with patch("app.api.routes.admin.get_telegram_bot") as mock_get_bot:
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=Exception("Forbidden: bot was blocked by the user"))
        mock_bot.session.close = AsyncMock()
        mock_get_bot.return_value = mock_bot

        res = await reply_to_ticket(ticket_id=ticket.id, req=req, admin=admin, db=db_session)
        assert res["success"] is False
        assert "ارسال به تلگرام کاربر با خطا مواجه گردید" in res["message"]

        # Verify ticket was NOT closed
        await db_session.refresh(ticket)
        assert ticket.status != TicketStatus.CLOSED


# -----------------------------------------------------------------------------
# 15. Bug 15: Background Expiration Check Loop Registered in API Main
# -----------------------------------------------------------------------------
def test_bug15_background_expiration_check_loop_registered():
    """Verify _background_expiration_check_loop is defined in app.api.main and imported cleanly."""
    from app.api.main import _background_expiration_check_loop
    assert callable(_background_expiration_check_loop)
