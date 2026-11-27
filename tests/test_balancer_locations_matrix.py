import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.services.balancer_location_service import BalancerLocationService, MASTER_LOCATION_CATALOG
from app.modules.pia.service import PiaSyncService
from app.modules.windscribe.service import WindscribeSyncService


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_and_default_21_active_locations(async_db):
    """Verifies that seed populates all catalog locations and the 21 Master locations are active."""
    added = await BalancerLocationService.initialize_and_seed(session=async_db)
    assert added == len(MASTER_LOCATION_CATALOG)

    locations = await BalancerLocationService.get_all_locations(session=async_db)
    assert len(locations) == len(MASTER_LOCATION_CATALOG)

    summary = await BalancerLocationService.get_summary(session=async_db)
    assert summary["total_locations"] == len(MASTER_LOCATION_CATALOG)
    assert summary["active_locations"] == 21
    assert summary["both_count"] > 0
    assert summary["pia_only_count"] > 0
    assert summary["wd_only_count"] > 0

    # Verify specific locations
    de_loc = next(l for l in locations if l["balancer_tag"] == "DE")
    assert de_loc["is_active"] is True
    assert de_loc["coverage_type"] == "BOTH"

    tr_loc = next(l for l in locations if l["balancer_tag"] == "TR")
    assert tr_loc["is_active"] is True
    assert tr_loc["coverage_type"] == "WD"

    yrm_loc = next(l for l in locations if l["balancer_tag"] == "YRM")
    assert yrm_loc["is_active"] is True
    assert yrm_loc["coverage_type"] == "PIA"

    jp_loc = next(l for l in locations if l["balancer_tag"] == "JP")
    assert jp_loc["is_active"] is False
    assert jp_loc["coverage_type"] == "BOTH"


@pytest.mark.asyncio
async def test_toggle_location_active_state(async_db):
    """Verifies toggling a location changes its active state and dynamic tag lists."""
    await BalancerLocationService.initialize_and_seed(session=async_db)

    # Initial state of JP is inactive
    jp_tags_initial = await BalancerLocationService.get_active_tags(session=async_db)
    assert "JP" not in jp_tags_initial

    # Toggle JP active
    res = await BalancerLocationService.toggle_location(balancer_tag="JP", is_active=True, session=async_db)
    assert res["success"] is True
    assert res["is_active"] is True

    # Now JP should be in active tags
    active_tags = await BalancerLocationService.get_active_tags(session=async_db)
    assert "JP" in active_tags

    summary = await BalancerLocationService.get_summary(session=async_db)
    assert summary["active_locations"] == 22


@pytest.mark.asyncio
async def test_provider_filter_active_tags(async_db):
    """Verifies provider-specific active tags filtering."""
    await BalancerLocationService.initialize_and_seed(session=async_db)

    # PIA active tags should not contain WD-only locations like TR, SE
    pia_tags = await BalancerLocationService.get_active_tags(provider="pia", session=async_db)
    assert "TR" not in pia_tags
    assert "SE" not in pia_tags
    assert "DE" in pia_tags
    assert "US-BL" in pia_tags
    assert "az" in pia_tags

    # Windscribe active tags should not contain PIA-only locations like az, YRM, EG
    wd_tags = await BalancerLocationService.get_active_tags(provider="windscribe", session=async_db)
    assert "az" not in wd_tags
    assert "YRM" not in wd_tags
    assert "TR" in wd_tags
    assert "SE" in wd_tags
    assert "DE" in wd_tags


@pytest.mark.asyncio
async def test_sync_services_target_balancers():
    """Verifies PiaSyncService and WindscribeSyncService dynamically load target balancers."""
    pia_balancers = PiaSyncService.get_target_balancers()
    assert isinstance(pia_balancers, list)
    assert "DE" in pia_balancers
    assert "US-BL" in pia_balancers

    wd_balancers = WindscribeSyncService.get_target_balancers()
    assert isinstance(wd_balancers, list)
    assert "TR" in wd_balancers
    assert "SE" in wd_balancers
    assert "DE" in wd_balancers
