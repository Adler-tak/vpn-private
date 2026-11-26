"""Unit and Integration tests for Plan Location Modes (all, custom, user_choice) and Bot Admin location management."""
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.database.session import Base
import app.database.models
from app.database.models.product import Product
from app.database.models.panel import Server, Panel, Inbound
from app.database.models.user import User
from app.core.constants import UserRole
from app.api.routes.admin import (
    ProductCreateRequest,
    ProductUpdateRequest,
    create_product,
    update_product,
    list_products,
)
from app.bot.handlers.purchase import handle_product_selected
from app.bot.handlers.admin import (
    view_admin_product_detail,
    view_admin_product_location_menu,
    admin_set_product_location_mode,
    admin_toggle_product_location_item,
)
from app.bot.states.customer import PurchaseOrderState


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
async def sample_admin_and_servers(db_session: AsyncSession):
    admin = User(
        id=uuid.uuid4(),
        telegram_id=9998881,
        username="admin_super",
        role=UserRole.SUPER_ADMIN,
        referral_code="ref_admin_test",
        is_active=True,
    )
    panel = Panel(
        id=uuid.uuid4(),
        name="Main-3XUI",
        panel_type="3x-ui",
        base_url="https://panel.test:2053",
        username="admin",
        _password_encrypted="encrypted_hash",
        is_active=True,
        is_healthy=True,
    )
    db_session.add_all([admin, panel])
    await db_session.flush()

    srv1 = Server(
        id=uuid.uuid4(),
        name="Server Germany",
        location_code="DE",
        location_name="Germany",
        flag_emoji="🇩🇪",
        panel_id=panel.id,
        subscription_host="de.test.com",
        is_active=True,
    )
    srv2 = Server(
        id=uuid.uuid4(),
        name="Server Netherlands",
        location_code="NL",
        location_name="Netherlands",
        flag_emoji="🇳🇱",
        panel_id=panel.id,
        subscription_host="nl.test.com",
        is_active=True,
    )
    db_session.add_all([srv1, srv2])
    await db_session.commit()
    return {"admin": admin, "panel": panel, "srv1": srv1, "srv2": srv2}


@pytest.mark.asyncio
async def test_web_api_product_crud_with_location_mode(db_session: AsyncSession, sample_admin_and_servers):
    admin = sample_admin_and_servers["admin"]

    # 1. Create with location_mode="all"
    req_all = ProductCreateRequest(
        name="VIP Multi-Location",
        traffic_gb=50.0,
        duration_days=30,
        price_toman=100000,
        protocol="vless",
        allowed_locations="ALL",
        location_mode="all",
    )
    res_all = await create_product(req_all, admin=admin, db=db_session)
    assert res_all["success"] is True
    prod_id = uuid.UUID(res_all["product"]["id"])

    # 2. Update to "custom" with specific locations
    req_up = ProductUpdateRequest(
        location_mode="custom",
        allowed_locations="DE,NL",
    )
    res_up = await update_product(prod_id, req_up, admin=admin, db=db_session)
    assert res_up["success"] is True

    # 3. List products and verify location_mode
    list_res = await list_products(admin=admin, db=db_session)
    prod_item = next(p for p in list_res["items"] if p["id"] == str(prod_id))
    assert prod_item["location_mode"] == "custom"
    assert prod_item["allowed_locations"] == "DE,NL"

    # 4. Update to "user_choice"
    req_choice = ProductUpdateRequest(location_mode="user_choice")
    await update_product(prod_id, req_choice, admin=admin, db=db_session)
    db_prod = await db_session.get(Product, prod_id)
    assert db_prod.location_mode == "user_choice"


@pytest.mark.asyncio
async def test_bot_purchase_flow_modes(db_session: AsyncSession, sample_admin_and_servers):
    # Case A: Multi-location plan (location_mode == 'all') -> NO asking location!
    prod_all = Product(
        name="Plan All",
        traffic_bytes=50 * (1024**3),
        duration_days=30,
        price_irr=1000000,
        protocol="vless",
        allowed_locations="ALL",
        location_mode="all",
        is_active=True,
    )
    # Case B: Multi-location custom plan (location_mode == 'custom') -> NO asking location!
    prod_custom = Product(
        name="Plan Custom DE-NL",
        traffic_bytes=50 * (1024**3),
        duration_days=30,
        price_irr=1200000,
        protocol="vless",
        allowed_locations="DE,NL",
        location_mode="custom",
        is_active=True,
    )
    # Case C: Single location plan (location_mode == 'user_choice') -> ASKS user!
    prod_choice = Product(
        name="Plan User Choice",
        traffic_bytes=30 * (1024**3),
        duration_days=30,
        price_irr=800000,
        protocol="vless",
        allowed_locations="DE,NL",
        location_mode="user_choice",
        is_active=True,
    )
    db_session.add_all([prod_all, prod_custom, prod_choice])
    await db_session.commit()

    # Test Case A: Plan All
    cb_a = AsyncMock()
    cb_a.data = f"buy_prod:{prod_all.id}"
    cb_a.message = AsyncMock()
    state_a = AsyncMock()
    await handle_product_selected(cb_a, state_a, db_session)
    # Must advance to entering_coupon directly without asking location
    state_a.set_state.assert_called_with(PurchaseOrderState.entering_coupon)
    state_a.update_data.assert_any_call(location_code="ALL")

    # Test Case B: Plan Custom (2 locations DE, NL)
    cb_b = AsyncMock()
    cb_b.data = f"buy_prod:{prod_custom.id}"
    cb_b.message = AsyncMock()
    state_b = AsyncMock()
    await handle_product_selected(cb_b, state_b, db_session)
    # Must advance to entering_coupon directly without asking location
    state_b.set_state.assert_called_with(PurchaseOrderState.entering_coupon)
    state_b.update_data.assert_any_call(location_code="DE,NL")

    # Test Case C: Plan User Choice (2 locations DE, NL)
    cb_c = AsyncMock()
    cb_c.data = f"buy_prod:{prod_choice.id}"
    cb_c.message = AsyncMock()
    state_c = AsyncMock()
    await handle_product_selected(cb_c, state_c, db_session)
    # Must ask location
    state_c.set_state.assert_called_with(PurchaseOrderState.selecting_location)


@pytest.mark.asyncio
async def test_telegram_bot_admin_location_menu(db_session: AsyncSession, sample_admin_and_servers):
    admin = sample_admin_and_servers["admin"]
    prod = Product(
        name="Test Admin Bot Loc",
        traffic_bytes=20 * (1024**3),
        duration_days=30,
        price_irr=500000,
        protocol="vless",
        allowed_locations="ALL",
        location_mode="all",
        is_active=True,
    )
    db_session.add(prod)
    await db_session.commit()

    # 1. View detail
    cb = AsyncMock()
    cb.data = f"admin_prod_detail:{prod.id}"
    cb.message = AsyncMock()
    await view_admin_product_detail(cb, admin, db_session)
    cb.message.edit_text.assert_called()
    sent_text = cb.message.edit_text.call_args[0][0]
    assert "نحوه تحویل:" in sent_text

    # 2. View location menu
    cb_loc = AsyncMock()
    cb_loc.data = f"admin_prod_loc_menu:{prod.id}"
    cb_loc.message = AsyncMock()
    await view_admin_product_location_menu(cb_loc, admin, db_session)
    cb_loc.message.edit_text.assert_called()

    # 3. Switch mode to 'custom'
    cb_mode = AsyncMock()
    cb_mode.data = f"admin_set_loc_mode:{prod.id}:custom"
    cb_mode.message = AsyncMock()
    await admin_set_product_location_mode(cb_mode, admin, db_session)
    await db_session.refresh(prod)
    assert prod.location_mode == "custom"

    # 4. Toggle server location 'DE'
    cb_tog = AsyncMock()
    cb_tog.data = f"admin_toggle_prod_loc:{prod.id}:DE"
    cb_tog.message = AsyncMock()
    await admin_toggle_product_location_item(cb_tog, admin, db_session)
    await db_session.refresh(prod)
    assert "DE" in prod.allowed_locations
