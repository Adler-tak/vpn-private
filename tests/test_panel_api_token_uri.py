import pytest
from app.integrations.xui.client import normalize_panel_base_url, ThreeXUIClient
from app.api.routes.admin import PanelCreateRequest, PanelUpdateRequest
from app.database.models.panel import Panel

def test_normalize_panel_base_url():
    # 1. URL without uri_path
    assert normalize_panel_base_url("https://zi.miladrvn.com:2053") == "https://zi.miladrvn.com:2053"
    assert normalize_panel_base_url("zi.miladrvn.com/panel/inbounds") == "https://zi.miladrvn.com"
    
    # 2. URL with separate uri_path
    assert normalize_panel_base_url("https://zi.miladrvn.com", "/iuvL2HXvrwROLgSMhf4uRcYAAA/") == "https://zi.miladrvn.com/iuvL2HXvrwROLgSMhf4uRcYAAA"
    assert normalize_panel_base_url("https://zi.miladrvn.com/", "iuvL2HXvrwROLgSMhf4uRcYAAA") == "https://zi.miladrvn.com/iuvL2HXvrwROLgSMhf4uRcYAAA"
    
    # 3. URL already having uri_path should not duplicate
    assert normalize_panel_base_url("https://zi.miladrvn.com/iuvL2HXvrwROLgSMhf4uRcYAAA", "/iuvL2HXvrwROLgSMhf4uRcYAAA/") == "https://zi.miladrvn.com/iuvL2HXvrwROLgSMhf4uRcYAAA"

def test_panel_model_token_and_uri():
    # When initialized with only api_token
    p = Panel(
        name="TokenPanel",
        base_url="https://panel.example.com/subpath/",
        api_token="my_secret_token_123"
    )
    assert p.username == "token_user"
    assert p.api_token == "my_secret_token_123"
    assert p.uri_path == "/subpath/"
    assert p.web_base_path == "/subpath/"

def test_panel_requests_validation():
    # 1. Request with only api_token and uri_path
    req1 = PanelCreateRequest(
        name="Panel 1",
        base_url="https://zi.miladrvn.com",
        uri_path="/customPath/",
        api_token="valid_jwt_token"
    )
    assert req1.api_token == "valid_jwt_token"
    assert req1.uri_path == "/customPath/"
    assert req1.username is None

    # 2. Request with username/password
    req2 = PanelCreateRequest(
        name="Panel 2",
        base_url="https://zi.miladrvn.com",
        username="admin",
        password="password123"
    )
    assert req2.username == "admin"
    assert req2.password == "password123"
