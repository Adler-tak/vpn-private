"""Factory functions to instantiate appropriate panel clients and adapters."""
from __future__ import annotations
import logging
from typing import Any, Optional, Union

from app.core.constants import PanelType
from app.database.models.panel import Panel
import app.integrations.vpn_ui.adapter as vpn_adapter_module
import app.integrations.vpn_ui.client as vpn_client_module
import app.integrations.xui.adapter as xui_adapter_module
import app.integrations.xui.client as xui_client_module
from app.integrations.vpn_ui.adapter import VpnUIPanelAdapter
from app.integrations.vpn_ui.client import VpnUIClient
from app.integrations.xui.adapter import ThreeXUIPanelAdapter
from app.integrations.xui.client import ThreeXUIClient

logger = logging.getLogger(__name__)


def _is_vpn_ui(panel: Any) -> bool:
    """Helper to detect if panel is marked as vpn-ui."""
    if panel is None:
        return False
    if isinstance(panel, dict):
        panel_type = panel.get("panel_type", "3x-ui")
    else:
        panel_type = getattr(panel, "panel_type", "3x-ui")
    if hasattr(panel_type, "value"):
        panel_type = panel_type.value
    return str(panel_type or "").strip().lower() in (
        PanelType.VPN_UI.value,
        "vpn-ui",
        "vpn_ui",
    )


def get_panel_client(
    panel: Union[Panel, Any],
    timeout: float = 15.0,
    verify_ssl: bool = False,
) -> Union[VpnUIClient, ThreeXUIClient]:
    """
    Instantiate and return the appropriate panel HTTP client.
    Returns VpnUIClient if panel_type == 'vpn-ui', else ThreeXUIClient.
    """
    if panel is None:
        raise ValueError("Panel configuration cannot be None")

    if isinstance(panel, (VpnUIClient, ThreeXUIClient)):
        return panel

    if isinstance(panel, dict):
        base_url = str(panel.get("base_url") or "")
        username = str(panel.get("username") or "")
        password = str(panel.get("password") or "")
        api_token = panel.get("api_token")
        login_secret = panel.get("login_secret")
        otp_secret = panel.get("otp_secret")
        name = panel.get("name", base_url)
    else:
        base_url = str(getattr(panel, "base_url", "") or "")
        username = str(getattr(panel, "username", "") or "")
        password = str(getattr(panel, "password", "") or "")
        api_token = getattr(panel, "api_token", None)
        login_secret = getattr(panel, "login_secret", None)
        otp_secret = getattr(panel, "otp_secret", None)
        name = getattr(panel, "name", base_url)

    if _is_vpn_ui(panel):
        logger.debug(f"Factory: creating VpnUIClient for panel '{name}'")
        vpn_cls = getattr(vpn_client_module, "VpnUIClient", VpnUIClient)
        return vpn_cls(
            base_url=base_url,
            username=username,
            password=password,
            api_token=api_token,
            login_secret=login_secret,
            otp_secret=otp_secret,
            timeout=timeout,
            verify_ssl=verify_ssl,
        )

    logger.debug(f"Factory: creating ThreeXUIClient for panel '{name}'")
    xui_cls = getattr(xui_client_module, "ThreeXUIClient", ThreeXUIClient)
    return xui_cls(
        base_url=base_url,
        username=username,
        password=password,
        api_token=api_token,
        login_secret=login_secret,
        otp_secret=otp_secret,
        timeout=timeout,
        verify_ssl=verify_ssl,
    )


def get_panel_adapter(
    panel: Union[Panel, Any],
    timeout: float = 15.0,
    verify_ssl: bool = False,
) -> ThreeXUIPanelAdapter:
    """
    Inspects panel_type and returns VpnUIPanelAdapter or ThreeXUIPanelAdapter.
    """
    if panel is None:
        raise ValueError("Panel configuration cannot be None")

    if isinstance(panel, VpnUIPanelAdapter):
        return panel
    if isinstance(panel, ThreeXUIPanelAdapter):
        return panel

    client = get_panel_client(panel, timeout=timeout, verify_ssl=verify_ssl)
    if isinstance(client, VpnUIClient):
        vpn_ad_cls = getattr(vpn_adapter_module, "VpnUIPanelAdapter", VpnUIPanelAdapter)
        return vpn_ad_cls(client=client)
    xui_ad_cls = getattr(xui_adapter_module, "ThreeXUIPanelAdapter", ThreeXUIPanelAdapter)
    return xui_ad_cls(client=client)
