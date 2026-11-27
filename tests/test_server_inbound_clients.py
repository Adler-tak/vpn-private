import pytest
from httpx import AsyncClient, ASGITransport
from app.api.main import app
from app.api.auth import get_current_admin
from app.database.session import AsyncSessionLocal
from app.database.models.user import User
from app.database.models.panel import Inbound, Panel, Server
import uuid

@pytest.fixture
def mock_admin():
    admin = User(
        id=uuid.uuid4(),
        telegram_id=99999999,
        username="test_admin",
        role="ADMIN",
        is_active=True,
    )
    return admin

@pytest.mark.asyncio
async def test_server_clients_endpoint(mock_admin):
    app.dependency_overrides[get_current_admin] = lambda: mock_admin

    async with AsyncSessionLocal() as db:
        p = Panel(
            id=uuid.uuid4(),
            name=f"Test Panel Server {uuid.uuid4().hex[:6]}",
            base_url="https://panel.test.com",
            username="admin",
            _password_encrypted="dummy_encrypted_password",
            is_active=True,
        )
        db.add(p)
        await db.flush()

        s_name = f"Test Germany Server {uuid.uuid4().hex[:6]}"
        server = Server(
            id=uuid.uuid4(),
            panel_id=p.id,
            name=s_name,
            location_code="DE",
            location_name="Germany",
            flag_emoji="🇩🇪",
            subscription_host="de.test.com",
            subscription_port=443,
            is_active=True,
            current_clients=1,
            capacity_limit=100,
        )
        db.add(server)
        await db.commit()
        await db.refresh(server)
        server_id = str(server.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get(f"/api/v1/admin/servers/{server_id}/clients")
        assert res.status_code == 200
        data = res.json()
        assert data["server_id"] == server_id
        assert data["server_name"] == s_name
        assert "clients" in data
        assert isinstance(data["clients"], list)

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_inbound_clients_endpoint(mock_admin):
    app.dependency_overrides[get_current_admin] = lambda: mock_admin

    async with AsyncSessionLocal() as db:
        p = Panel(
            id=uuid.uuid4(),
            name=f"Test Panel Inbound {uuid.uuid4().hex[:6]}",
            base_url="https://panel.test.com",
            username="admin",
            _password_encrypted="dummy_encrypted_password",
            is_active=True,
        )
        db.add(p)
        await db.flush()

        inbound = Inbound(
            id=uuid.uuid4(),
            panel_id=p.id,
            remote_inbound_id=10,
            tag="VLESS-TCP-REALITY",
            port=443,
            protocol="vless",
            is_active=True,
        )
        db.add(inbound)
        await db.commit()
        await db.refresh(inbound)
        inbound_id = str(inbound.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get(f"/api/v1/admin/inbounds/{inbound_id}/clients")
        assert res.status_code == 200
        data = res.json()
        assert data["inbound_id"] == inbound_id
        assert data["remote_id"] == 10
        assert "clients" in data

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_list_coupons_fields(mock_admin):
    from app.database.models.reseller import Coupon
    app.dependency_overrides[get_current_admin] = lambda: mock_admin

    async with AsyncSessionLocal() as db:
        c = Coupon(
            id=uuid.uuid4(),
            code=f"TEST{uuid.uuid4().hex[:4].upper()}",
            discount_percent=25,
            max_uses=50,
            current_uses=5,
            is_active=True,
        )
        db.add(c)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/v1/admin/coupons")
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        found = next((item for item in data if item["code"] == c.code), None)
        assert found is not None
        assert found["current_uses"] == 5
        assert found["used_count"] == 5
        assert found["max_uses"] == 50

    app.dependency_overrides.clear()
