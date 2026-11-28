"""Asynchronous HTTP client for VPN-UI panels (WireGuard, AmneziaWG, OpenVPN, and 3x-ui)."""
import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

from app.core.constants import PanelType, ProtocolType
from app.core.exceptions import PanelError
from app.integrations.vpn_ui.wg_config import generate_wireguard_keypair
from app.integrations.xui.client import ThreeXUIClient

logger = logging.getLogger(__name__)


class VpnUIClient(ThreeXUIClient):
    """
    Extended panel client supporting Sir-MmD/vpn-ui features while maintaining
    100% backward compatibility with standard 3x-ui panels.
    """

    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_token: Optional[str] = None,
        login_secret: Optional[str] = None,
        otp_secret: Optional[str] = None,
        timeout: float = 15.0,
        verify_ssl: bool = False,
        uri_path: Optional[str] = None,
    ):
        super().__init__(
            base_url=base_url,
            username=username,
            password=password,
            api_token=api_token,
            login_secret=login_secret,
            otp_secret=otp_secret,
            timeout=timeout,
            verify_ssl=verify_ssl,
            uri_path=uri_path,
        )
        self._panel_type_cache: Optional[str] = None

    # -----------------------------------------------------------------------
    # Panel Type Detection & Capabilities
    # -----------------------------------------------------------------------

    async def detect_panel_type(self, force_refresh: bool = False) -> str:
        """
        Inspect the remote panel to determine if it is a VPN-UI panel or standard 3x-ui.
        Caches the result unless force_refresh=True.
        Returns:
            PanelType.VPN_UI ("vpn-ui") or PanelType.THREE_X_UI ("3x-ui")
        """
        if self._panel_type_cache and not force_refresh:
            return self._panel_type_cache

        detected = PanelType.THREE_X_UI.value

        # 1. Probe dedicated VPN-UI API endpoints
        try:
            res = await self._request("GET", "/panel/api/vpn-ui/status")
            if res.get("success", False):
                self._panel_type_cache = PanelType.VPN_UI.value
                return self._panel_type_cache
        except Exception:
            pass

        # 2. Check server status metadata for VPN-UI version / tags
        try:
            status = await self.get_status()
            obj = status.get("obj") if isinstance(status, dict) else {}
            if isinstance(obj, dict):
                raw_str = json.dumps(obj, default=str).lower()
                if any(term in raw_str for term in ("vpn-ui", "vpn_ui", "amneziawg", "sir-mmd")):
                    self._panel_type_cache = PanelType.VPN_UI.value
                    return self._panel_type_cache
        except Exception as e:
            logger.debug(f"Status check during detect_panel_type: {e}")

        # 3. Check inbounds protocols for wireguard / amneziawg / openvpn
        try:
            inbounds = await self.get_inbounds()
            if isinstance(inbounds, list):
                for inb in inbounds:
                    if not isinstance(inb, dict):
                        continue
                    proto = (inb.get("protocol") or "").strip().lower()
                    if proto in (
                        ProtocolType.WIREGUARD.value,
                        ProtocolType.AMNEZIAWG.value,
                        ProtocolType.OPENVPN.value,
                        "awg",
                    ):
                        self._panel_type_cache = PanelType.VPN_UI.value
                        return self._panel_type_cache

                    # Also inspect settings/streamSettings for AWG or OpenVPN signatures
                    settings_str = str(inb.get("settings", "")).lower()
                    if any(x in settings_str for x in ("jc", "jmin", "jmax", "s1", "s2", "amnezia")):
                        self._panel_type_cache = PanelType.VPN_UI.value
                        return self._panel_type_cache
        except Exception as e:
            logger.debug(f"Inbounds check during detect_panel_type: {e}")

        # 4. Check HTML homepage / login page for VPN-UI branding
        try:
            client = await self._get_client()
            resp = await client.get(self.base_url + "/")
            if resp.status_code == 200:
                html_lower = resp.text.lower()
                if any(term in html_lower for term in ("vpn-ui", "vpn ui", "sir-mmd", "amneziawg")):
                    self._panel_type_cache = PanelType.VPN_UI.value
                    return self._panel_type_cache
        except Exception as e:
            logger.debug(f"HTML branding probe during detect_panel_type: {e}")

        self._panel_type_cache = detected
        return detected

    async def get_capabilities(self) -> Dict[str, Any]:
        """Query and return active panel protocol and engine capabilities."""
        panel_type = await self.detect_panel_type()
        inbounds = await self.get_inbounds()
        if not isinstance(inbounds, list):
            inbounds = []
        protocols = {
            (inb.get("protocol") or "").lower()
            for inb in inbounds
            if isinstance(inb, dict) and inb.get("protocol")
        }

        return {
            "panel_type": panel_type,
            "is_vpn_ui": panel_type == PanelType.VPN_UI.value,
            "has_wireguard": "wireguard" in protocols or panel_type == PanelType.VPN_UI.value,
            "has_amneziawg": any(p in protocols for p in ("amneziawg", "awg")) or panel_type == PanelType.VPN_UI.value,
            "has_openvpn": "openvpn" in protocols or panel_type == PanelType.VPN_UI.value,
            "active_protocols": sorted(list(protocols)),
            "inbounds_count": len(inbounds),
        }

    # -----------------------------------------------------------------------
    # Protocol Inbounds Queries
    # -----------------------------------------------------------------------

    async def get_inbounds_by_protocol(self, protocol: str) -> List[Dict[str, Any]]:
        """Filter panel inbounds matching specified protocol string."""
        inbounds = await self.get_inbounds()
        if not isinstance(inbounds, list):
            return []
        proto_lower = protocol.strip().lower()
        matched = []
        for inb in inbounds:
            if not isinstance(inb, dict):
                continue
            p = (inb.get("protocol") or "").strip().lower()
            if p == proto_lower:
                matched.append(inb)
            # Match aliases
            elif proto_lower in ("amneziawg", "awg") and p in ("amneziawg", "awg"):
                matched.append(inb)
        return matched

    async def get_wireguard_inbounds(self) -> List[Dict[str, Any]]:
        """List all WireGuard inbounds configured on panel."""
        return await self.get_inbounds_by_protocol(ProtocolType.WIREGUARD.value)

    async def get_amneziawg_inbounds(self) -> List[Dict[str, Any]]:
        """List all AmneziaWG (AWG) inbounds configured on panel."""
        return await self.get_inbounds_by_protocol(ProtocolType.AMNEZIAWG.value)

    async def get_openvpn_inbounds(self) -> List[Dict[str, Any]]:
        """List all OpenVPN inbounds configured on panel."""
        return await self.get_inbounds_by_protocol(ProtocolType.OPENVPN.value)

    # -----------------------------------------------------------------------
    # WireGuard Client Operations
    # -----------------------------------------------------------------------

    async def add_wireguard_client(self, inbound_id: int, client_spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add a WireGuard peer/client to the specified inbound.
        Auto-generates Curve25519 keypair if missing.
        """
        spec = dict(client_spec)
        if not spec.get("publicKey") and not spec.get("public_key"):
            priv, pub = generate_wireguard_keypair()
            spec["privateKey"] = priv
            spec["publicKey"] = pub
        elif not spec.get("publicKey") and spec.get("public_key"):
            spec["publicKey"] = spec["public_key"]

        # Normalize field names for VPN-UI WireGuard peer
        if "address" in spec and "allowedIPs" not in spec:
            spec["allowedIPs"] = spec["address"]

        # 1. Try modern /panel/api/clients/add
        try:
            return await self.add_client(inbound_id, spec)
        except PanelError as exc:
            # 2. Try legacy inbound update with peers payload
            logger.info(f"add_client fallback for WireGuard inbound {inbound_id}: {exc}")
            peers_payload = json.dumps({"peers": [spec]})
            payload = {"id": inbound_id, "settings": peers_payload}
            try:
                return await self._request("POST", "/panel/api/inbounds/addClient", json_data=payload)
            except PanelError:
                # 3. Direct inbound update if client add not supported
                inbound = await self.get_inbound(inbound_id)
                if not inbound or not isinstance(inbound, dict):
                    raise PanelError(f"Inbound {inbound_id} not found or invalid")
                settings = inbound.get("settings", {})
                if isinstance(settings, str):
                    try:
                        settings = json.loads(settings)
                    except Exception:
                        settings = {}
                if not isinstance(settings, dict):
                    settings = {}
                peers = settings.get("peers")
                if not isinstance(peers, list):
                    peers = []
                peers.append(spec)
                settings["peers"] = peers
                update_payload = {"id": inbound_id, "settings": json.dumps(settings)}
                return await self._request("POST", f"/panel/api/inbounds/update/{inbound_id}", json_data=update_payload)

    async def update_wireguard_client(
        self, inbound_id: int, client_key_or_uuid: str, client_spec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update existing WireGuard client/peer parameters."""
        spec = dict(client_spec)
        try:
            return await self.update_client(inbound_id, client_key_or_uuid, spec)
        except PanelError as exc:
            logger.info(f"update_client fallback for WireGuard peer {client_key_or_uuid}: {exc}")
            peers_payload = json.dumps({"peers": [spec]})
            payload = {"id": inbound_id, "settings": peers_payload}
            return await self._request(
                "POST", f"/panel/api/inbounds/updateClient/{client_key_or_uuid}", json_data=payload
            )

    async def delete_wireguard_client(
        self, inbound_id: int, client_key_or_uuid: str, client_email: Optional[str] = None
    ) -> Dict[str, Any]:
        """Remove a WireGuard peer from panel."""
        return await self.delete_client(inbound_id, client_key_or_uuid, client_email=client_email)

    # -----------------------------------------------------------------------
    # AmneziaWG (AWG) Client Operations
    # -----------------------------------------------------------------------

    async def add_amneziawg_client(self, inbound_id: int, client_spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add an AmneziaWG peer/client to the specified inbound.
        Preserves AWG obfuscation headers if supplied in client_spec.
        """
        spec = dict(client_spec)
        if not spec.get("publicKey") and not spec.get("public_key"):
            priv, pub = generate_wireguard_keypair()
            spec["privateKey"] = priv
            spec["publicKey"] = pub
        elif not spec.get("publicKey") and spec.get("public_key"):
            spec["publicKey"] = spec["public_key"]

        if "address" in spec and "allowedIPs" not in spec:
            spec["allowedIPs"] = spec["address"]

        return await self.add_wireguard_client(inbound_id, spec)

    async def update_amneziawg_client(
        self, inbound_id: int, client_key_or_uuid: str, client_spec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update existing AmneziaWG peer parameters."""
        return await self.update_wireguard_client(inbound_id, client_key_or_uuid, client_spec)

    async def delete_amneziawg_client(
        self, inbound_id: int, client_key_or_uuid: str, client_email: Optional[str] = None
    ) -> Dict[str, Any]:
        """Remove an AmneziaWG peer from panel."""
        return await self.delete_wireguard_client(inbound_id, client_key_or_uuid, client_email=client_email)

    # -----------------------------------------------------------------------
    # OpenVPN Client Operations
    # -----------------------------------------------------------------------

    async def add_openvpn_client(self, inbound_id: int, client_spec: Dict[str, Any]) -> Dict[str, Any]:
        """Add an OpenVPN client account to the specified inbound."""
        spec = dict(client_spec)
        # 1. Try standard /panel/api/clients/add
        try:
            return await self.add_client(inbound_id, spec)
        except PanelError as exc:
            logger.info(f"add_client fallback for OpenVPN inbound {inbound_id}: {exc}")
            # 2. Try legacy inbound update with openvpn users/clients
            users_payload = json.dumps({"clients": [spec], "users": [spec]})
            payload = {"id": inbound_id, "settings": users_payload}
            return await self._request("POST", "/panel/api/inbounds/addClient", json_data=payload)

    async def update_openvpn_client(
        self, inbound_id: int, client_id_or_username: str, client_spec: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update existing OpenVPN client credentials or limits."""
        return await self.update_client(inbound_id, client_id_or_username, client_spec)

    async def delete_openvpn_client(
        self, inbound_id: int, client_id_or_username: str, client_email: Optional[str] = None
    ) -> Dict[str, Any]:
        """Remove an OpenVPN client account from panel."""
        return await self.delete_client(inbound_id, client_id_or_username, client_email=client_email)
