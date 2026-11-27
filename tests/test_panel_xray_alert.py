"""Unit tests for PanelHealthService and Telegram Xray error alerting."""
import pytest
import uuid
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone, timedelta

from app.database.models.panel import Panel
from app.services.panel_health_service import PanelHealthService, _panel_alert_cache
from app.services.settings_service import SettingsService


@pytest.fixture(autouse=True)
def clean_cache():
    PanelHealthService.reset_cache_for_testing()
    yield
    PanelHealthService.reset_cache_for_testing()


@pytest.mark.asyncio
async def test_check_panel_health_healthy():
    """Test when 3X-UI login succeeds and xray is running with no errorMsg."""
    panel = Panel(
        id=uuid.uuid4(),
        name="Test Germany",
        base_url="https://panel.example.com:2053",
        username="admin",
        is_active=True,
    )
    panel.password = "secret123"

    with patch("app.services.panel_health_service.ThreeXUIClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.base_url = panel.base_url
        mock_client.username = panel.username
        mock_client.api_token = None
        mock_client._is_authenticated = False
        mock_client.login.return_value = True
        mock_client.get_status.return_value = {
            "success": True,
            "obj": {
                "xray": {
                    "state": "running",
                    "errorMsg": "",
                    "version": "1.8.24",
                }
            }
        }

        is_healthy, err_msg, details = await PanelHealthService.check_panel_health(panel, use_pool=False)
        assert is_healthy is True
        assert err_msg == ""
        assert details["xray_state"] == "running"
        assert details["xray_version"] == "1.8.24"


@pytest.mark.asyncio
async def test_check_panel_health_xray_stopped():
    """Test when xray state is not running."""
    panel = Panel(
        id=uuid.uuid4(),
        name="Test Finland",
        base_url="https://fi.example.com:2053",
        username="admin",
        is_active=True,
    )
    panel.password = "secret123"

    with patch("app.services.panel_health_service.ThreeXUIClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.base_url = panel.base_url
        mock_client.username = panel.username
        mock_client.api_token = None
        mock_client._is_authenticated = False
        mock_client.login.return_value = True
        mock_client.get_status.return_value = {
            "success": True,
            "obj": {
                "xray": {
                    "state": "stop",
                    "errorMsg": "",
                    "version": "1.8.24",
                }
            }
        }

        is_healthy, err_msg, details = await PanelHealthService.check_panel_health(panel, use_pool=False)
        assert is_healthy is False
        assert "متوقف است" in err_msg
        assert details["xray_state"] == "stop"


@pytest.mark.asyncio
async def test_check_panel_health_xray_error_msg():
    """Test when xray has an errorMsg."""
    panel = Panel(
        id=uuid.uuid4(),
        name="Test Sweden",
        base_url="https://se.example.com:2053",
        username="admin",
        is_active=True,
    )
    panel.password = "secret123"

    with patch("app.services.panel_health_service.ThreeXUIClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.base_url = panel.base_url
        mock_client.username = panel.username
        mock_client.api_token = None
        mock_client._is_authenticated = False
        mock_client.login.return_value = True
        mock_client.get_status.return_value = {
            "success": True,
            "obj": {
                "xray": {
                    "state": "error",
                    "errorMsg": "Failed to start: address already in use :443",
                    "version": "1.8.24",
                }
            }
        }

        is_healthy, err_msg, details = await PanelHealthService.check_panel_health(panel, use_pool=False)
        assert is_healthy is False
        assert "address already in use" in err_msg


@pytest.mark.asyncio
async def test_check_panel_health_connection_failure():
    """Test when connection to panel fails completely."""
    panel = Panel(
        id=uuid.uuid4(),
        name="Test Offline",
        base_url="https://down.example.com:2053",
        username="admin",
        is_active=True,
    )
    panel.password = "secret123"

    with patch("app.services.panel_health_service.ThreeXUIClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        mock_client.base_url = panel.base_url
        mock_client.username = panel.username
        mock_client.api_token = None
        mock_client._is_authenticated = False
        mock_client.login.side_effect = Exception("ConnectTimeout")

        is_healthy, err_msg, details = await PanelHealthService.check_panel_health(panel, use_pool=False)
        assert is_healthy is False
        assert "عدم پاسخگویی" in err_msg


@pytest.mark.asyncio
async def test_check_panel_health_with_retry_recovers():
    """Test that retry loop suppresses false alarm when panel recovers on attempt 2."""
    panel = Panel(
        id=uuid.uuid4(),
        name="Glitchy Node",
        base_url="https://glitch.example.com:2053",
        username="admin",
        is_active=True,
    )
    panel.password = "pwd"

    # Call 1: fails with 403, Call 2: succeeds
    health_calls = [
        (False, "خطای احراز هویت با پنل: Access forbidden (HTTP 403)", {}),
        (True, "", {"xray_state": "running", "xray_version": "1.8.24"}),
    ]

    with patch.object(PanelHealthService, "check_panel_health", side_effect=health_calls), \
         patch("asyncio.sleep", new_callable=AsyncMock):

        is_healthy, err, details = await PanelHealthService.check_panel_health_with_retry(
            panel=panel,
            retry_count=3,
            retry_interval=1,
        )
        assert is_healthy is True
        assert err == ""
        assert details.get("xray_state") == "running"


@pytest.mark.asyncio
async def test_check_panel_health_with_retry_all_fail():
    """Test that retry loop returns confirmed failure when all retries fail."""
    panel = Panel(
        id=uuid.uuid4(),
        name="Dead Node",
        base_url="https://dead.example.com:2053",
        username="admin",
        is_active=True,
    )
    panel.password = "pwd"

    with patch.object(PanelHealthService, "check_panel_health", return_value=(False, "Connection Refused", {})), \
         patch("asyncio.sleep", new_callable=AsyncMock):

        is_healthy, err, details = await PanelHealthService.check_panel_health_with_retry(
            panel=panel,
            retry_count=4,
            retry_interval=2,
        )
        assert is_healthy is False
        assert "Connection Refused" in err
        assert "4 بار تست متوالی" in err


@pytest.mark.asyncio
async def test_record_panel_result_alert_and_deduplication():
    """Test that Telegram notification is sent on first error, deduped within cooldown, and sends recovery alert."""
    panel = Panel(
        id=uuid.uuid4(),
        name="Node Sweden-01",
        base_url="https://se-01.whisker.com:2053",
        username="admin",
        is_active=True,
        is_healthy=True,
    )
    panel.password = "pwd"

    mock_db = AsyncMock()
    mock_bot = AsyncMock()

    with patch("app.services.panel_health_service.notify_all_admins", new_callable=AsyncMock) as mock_notify:
        # 1. First error occurrence -> alert must be dispatched
        alert_sent = await PanelHealthService.record_panel_result(
            panel=panel,
            is_healthy=False,
            error_msg="خطای هسته Xray: port 443 already bound",
            db_session=mock_db,
            bot=mock_bot,
        )
        assert alert_sent is True
        assert mock_notify.call_count == 1
        call_args = mock_notify.call_args[1]
        assert "Node Sweden-01" in call_args["text"]
        assert "port 443 already bound" in call_args["text"]
        assert panel.is_healthy is False

        # 2. Second error immediately -> cooldown prevents spam (no duplicate alert)
        mock_notify.reset_mock()
        alert_sent_2 = await PanelHealthService.record_panel_result(
            panel=panel,
            is_healthy=False,
            error_msg="خطای هسته Xray: port 443 already bound",
            db_session=mock_db,
            bot=mock_bot,
        )
        assert alert_sent_2 is False
        assert mock_notify.call_count == 0

        # 3. Third check after cooldown -> alert repeats
        cached = PanelHealthService.get_cached_state(panel.id)
        cached["last_alert_at"] = datetime.now(timezone.utc) - timedelta(seconds=3600)

        alert_sent_3 = await PanelHealthService.record_panel_result(
            panel=panel,
            is_healthy=False,
            error_msg="خطای هسته Xray: port 443 already bound",
            db_session=mock_db,
            bot=mock_bot,
        )
        assert alert_sent_3 is True
        assert mock_notify.call_count == 1

        # 4. Panel recovers -> recovery alert dispatched
        mock_notify.reset_mock()
        alert_sent_4 = await PanelHealthService.record_panel_result(
            panel=panel,
            is_healthy=True,
            error_msg="",
            db_session=mock_db,
            bot=mock_bot,
        )
        assert alert_sent_4 is True
        assert mock_notify.call_count == 1
        rec_call = mock_notify.call_args[1]
        assert "برطرف شدن خطا و اتصال مجدد Xray" in rec_call["text"]
        assert "Node Sweden-01" in rec_call["text"]
        assert panel.is_healthy is True
