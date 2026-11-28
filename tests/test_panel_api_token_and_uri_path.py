"""Unit tests for Panel API Token authentication and URI Path enhancements."""
import pytest
import uuid
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from app.database.models.panel import Panel
from app.integrations.xui.client import ThreeXUIClient, normalize_panel_base_url
from app.integrations.factory import get_panel_client
from app.api.routes.admin import PanelCreateRequest, PanelUpdateRequest


def test_normalize_panel_base_url_variations():
    """Test URL normalization with various base_url and uri_path combinations."""
    # 1. Clean base URL without uri_path
    assert normalize_panel_base_url("http://185.1.2.3:2053") == "http://185.1.2.3:2053"
    assert normalize_panel_base_url("https://panel.example.com/panel/inbounds") == "https://panel.example.com"
    assert normalize_panel_base_url("panel.example.com:2053") == "https://panel.example.com:2053"

    # 2. Base URL with uri_path
    assert normalize_panel_base_url("http://185.1.2.3:2053", uri_path="/subpath/") == "http://185.1.2.3:2053/subpath"
    assert normalize_panel_base_url("http://185.1.2.3:2053", uri_path="subpath") == "http://185.1.2.3:2053/subpath"
    assert normalize_panel_base_url("http://185.1.2.3:2053", uri_path="/deep/nested/path/") == "http://185.1.2.3:2053/deep/nested/path"

    # 3. Base URL already containing uri_path - should NOT duplicate!
    assert normalize_panel_base_url("http://185.1.2.3:2053/subpath", uri_path="/subpath/") == "http://185.1.2.3:2053/subpath"
    assert normalize_panel_base_url("http://185.1.2.3:2053/subpath/panel", uri_path="/subpath/") == "http://185.1.2.3:2053/subpath"
    assert normalize_panel_base_url("http://185.1.2.3:2053/subpath/inbounds", uri_path="subpath") == "http://185.1.2.3:2053/subpath"


def test_panel_model_api_token_and_uri_path():
    """Test Panel model handling of uri_path, web_base_path, and api_token dummy credentials."""
    # 1. Standard panel with username and password
    p1 = Panel(
        name="Panel 1",
        base_url="https://p1.example.com",
        username="admin",
        password="secretpassword",
        uri_path="/xui/",
    )
    assert p1.username == "admin"
    assert p1.password == "secretpassword"
    assert p1.uri_path == "/xui/"
    assert p1.web_base_path == "/xui/"

    # Test web_base_path setter alias
    p1.web_base_path = "/new_path/"
    assert p1.uri_path == "/new_path/"

    # 2. Panel with api_token and NO username/password supplied
    p2 = Panel(
        name="Token Panel",
        base_url="https://token.example.com",
        api_token="my_secret_bearer_token",
    )
    # Check fallback username and encrypted placeholder password
    assert p2.username == "token_user"
    assert p2._password_encrypted is not None
    assert p2.password == "API_TOKEN_AUTH"
    assert p2.api_token == "my_secret_bearer_token"

    # 3. Setting api_token property on existing panel with empty username/password
    p3 = Panel(
        name="Empty Panel",
        base_url="https://p3.example.com",
    )
    p3.api_token = "another_token_123"
    assert p3.username == "token_user"
    assert p3._password_encrypted is not None
    assert p3.password == "API_TOKEN_AUTH"


def test_three_xui_client_init_and_bearer_headers():
    """Test ThreeXUIClient constructor with uri_path and api_token."""
    client = ThreeXUIClient(
        base_url="http://10.0.0.1:2053",
        api_token="tok_abc123",
        uri_path="/custom_xui/",
    )
    assert client.base_url == "http://10.0.0.1:2053/custom_xui"
    assert client.uri_path == "/custom_xui/"
    assert client.api_token == "tok_abc123"
    assert client.username == ""
    assert client.password == ""


@pytest.mark.asyncio
async def test_three_xui_client_login_with_api_token():
    """Test ThreeXUIClient.login() authenticating using Bearer token without username/password."""
    client = ThreeXUIClient(
        base_url="https://panel.example.com",
        api_token="test_bearer_token_xyz",
        uri_path="/xui/",
    )

    with patch.object(client, "_get_client") as mock_get_client:
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.headers = {}
        mock_get_client.return_value = mock_http

        # Mock status endpoint returning success
        mock_status_resp = MagicMock()
        mock_status_resp.status_code = 200
        mock_status_resp.json.return_value = {"success": True, "obj": {"status": "ok"}}
        mock_http.get.return_value = mock_status_resp

        # Perform login
        logged_in = await client.login()
        assert logged_in is True
        assert client._is_authenticated is True
        assert mock_http.headers.get("Authorization") == "Bearer test_bearer_token_xyz"


@pytest.mark.asyncio
async def test_three_xui_client_endpoints_send_bearer_token():
    """Test that all endpoints pass Authorization: Bearer <token> on every request."""
    client = ThreeXUIClient(
        base_url="https://panel.example.com",
        api_token="secret_token_789",
    )

    with patch.object(client, "_get_client") as mock_get_client:
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.headers = {}
        mock_get_client.return_value = mock_http

        # Mock request response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"success": true, "obj": []}'
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"success": True, "obj": [{"id": 1, "protocol": "vless"}]}
        mock_http.request.return_value = mock_resp

        # Call get_inbounds
        client._is_authenticated = True
        inbounds = await client.get_inbounds()
        assert len(inbounds) == 1

        # Check call arguments to mock_http.request
        mock_http.request.assert_called()
        call_kwargs = mock_http.request.call_args.kwargs
        headers_sent = call_kwargs.get("headers")
        assert headers_sent is not None
        assert headers_sent.get("Authorization") == "Bearer secret_token_789"


def test_panel_request_schemas_validation():
    """Test PanelCreateRequest and PanelUpdateRequest with api_token and uri_path."""
    # 1. PanelCreateRequest with api_token and uri_path, omitting username/password
    req = PanelCreateRequest(
        name="Token Panel Schema",
        base_url="http://1.2.3.4:2053",
        api_token="token_val",
        uri_path="/mypath/",
    )
    assert req.api_token == "token_val"
    assert req.uri_path == "/mypath/"
    assert req.username is None
    assert req.password is None

    # 2. PanelUpdateRequest with uri_path and api_token
    upd = PanelUpdateRequest(
        uri_path="/new_uri/",
        api_token="upd_token",
    )
    assert upd.uri_path == "/new_uri/"
    assert upd.api_token == "upd_token"


def test_factory_get_panel_client_with_uri_path():
    """Test factory get_panel_client extracts uri_path and api_token properly."""
    panel_dict = {
        "base_url": "http://185.20.30.40:2053",
        "api_token": "factory_token_123",
        "uri_path": "/xui_base/",
        "panel_type": "3x-ui",
    }
    client = get_panel_client(panel_dict)
    assert isinstance(client, ThreeXUIClient)
    assert client.api_token == "factory_token_123"
    assert client.uri_path == "/xui_base/"
    assert client.base_url == "http://185.20.30.40:2053/xui_base"
