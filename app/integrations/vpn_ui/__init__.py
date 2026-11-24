"""VPN-UI (WireGuard, AmneziaWG, OpenVPN) integration package."""
from app.integrations.vpn_ui.adapter import VpnUIPanelAdapter
from app.integrations.vpn_ui.client import VpnUIClient
from app.integrations.vpn_ui.schemas import (
    AmneziaWGClientConfig,
    AmneziaWGInterface,
    AmneziaWGParams,
    OpenVPNClientSpec,
    OpenVPNProfile,
    WireGuardClientConfig,
    WireGuardClientSpec,
    WireGuardInterface,
    WireGuardPeer,
)
from app.integrations.vpn_ui.wg_config import (
    ConfigParseError,
    generate_awg_conf,
    generate_awg_url,
    generate_openvpn_conf,
    generate_preshared_key,
    generate_wireguard_conf,
    generate_wireguard_keypair,
    generate_wireguard_url,
    get_public_key_from_private,
    parse_awg_conf,
    parse_openvpn_conf,
    parse_wireguard_conf,
)

__all__ = [
    "VpnUIClient",
    "VpnUIPanelAdapter",
    "AmneziaWGParams",
    "WireGuardInterface",
    "AmneziaWGInterface",
    "WireGuardPeer",
    "WireGuardClientConfig",
    "AmneziaWGClientConfig",
    "OpenVPNProfile",
    "WireGuardClientSpec",
    "OpenVPNClientSpec",
    "ConfigParseError",
    "generate_wireguard_keypair",
    "get_public_key_from_private",
    "generate_preshared_key",
    "generate_wireguard_conf",
    "parse_wireguard_conf",
    "generate_awg_conf",
    "parse_awg_conf",
    "generate_openvpn_conf",
    "parse_openvpn_conf",
    "generate_wireguard_url",
    "generate_awg_url",
]
