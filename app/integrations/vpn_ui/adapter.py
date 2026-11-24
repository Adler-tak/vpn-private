"""High-level VPN-UI panel adapter supporting WireGuard, AmneziaWG, and OpenVPN."""
import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional, Union

from app.core.constants import ProtocolType
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
    generate_awg_conf,
    generate_awg_url,
    generate_openvpn_conf,
    generate_wireguard_conf,
    generate_wireguard_keypair,
    generate_wireguard_url,
)
from app.integrations.xui.adapter import ThreeXUIPanelAdapter
from app.integrations.xui.schemas import InboundData

logger = logging.getLogger(__name__)


class VpnUIPanelAdapter(ThreeXUIPanelAdapter):
    """
    Adapter extending ThreeXUIPanelAdapter with complete support for
    WireGuard, AmneziaWG (AWG), and OpenVPN configurations and links.
    """

    def __init__(self, client: VpnUIClient):
        super().__init__(client=client)
        self.vpn_client: VpnUIClient = client

    # -----------------------------------------------------------------------
    # Inbounds Queries by Protocol
    # -----------------------------------------------------------------------

    async def list_wireguard_inbounds(self) -> List[InboundData]:
        """List all WireGuard inbounds as typed InboundData models."""
        all_inbounds = await self.list_inbounds()
        return [inb for inb in all_inbounds if inb.protocol.lower() == ProtocolType.WIREGUARD.value]

    async def list_amneziawg_inbounds(self) -> List[InboundData]:
        """List all AmneziaWG (AWG) inbounds as typed InboundData models."""
        all_inbounds = await self.list_inbounds()
        return [
            inb for inb in all_inbounds
            if inb.protocol.lower() in (ProtocolType.AMNEZIAWG.value, "awg")
        ]

    async def list_openvpn_inbounds(self) -> List[InboundData]:
        """List all OpenVPN inbounds as typed InboundData models."""
        all_inbounds = await self.list_inbounds()
        return [inb for inb in all_inbounds if inb.protocol.lower() == ProtocolType.OPENVPN.value]

    # -----------------------------------------------------------------------
    # Configuration File Generation Methods
    # -----------------------------------------------------------------------

    def _extract_settings_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Safely parse and return settings dict from inbound data."""
        if not isinstance(data, dict):
            return {}
        settings = data.get("settings", {})
        if isinstance(settings, str):
            try:
                res = json.loads(settings)
                return res if isinstance(res, dict) else {}
            except Exception:
                return {}
        elif isinstance(settings, dict):
            return settings
        return {}

    def _extract_server_public_key(self, inbound_data: Dict[str, Any]) -> str:
        """Extract server Curve25519 public key from inbound data/settings."""
        if not isinstance(inbound_data, dict):
            return "SERVER_PUBLIC_KEY_PLACEHOLDER"
        settings = self._extract_settings_dict(inbound_data)
        # VPN-UI / Xray wireguard inbounds store public key or secret key
        pub = (
            settings.get("publicKey")
            or settings.get("serverPublicKey")
            or inbound_data.get("publicKey")
            or inbound_data.get("public_key")
        )
        if pub:
            return str(pub).strip()
        return "SERVER_PUBLIC_KEY_PLACEHOLDER"

    def _extract_awg_params(
        self, inbound_data: Dict[str, Any], client_data: Dict[str, Any]
    ) -> AmneziaWGParams:
        """Extract AWG obfuscation parameters from inbound settings or client data."""
        if not isinstance(client_data, dict):
            client_data = {}
        if not isinstance(inbound_data, dict):
            inbound_data = {}

        # 1. From client_data direct awg_params
        if isinstance(client_data.get("awg_params"), AmneziaWGParams):
            return client_data["awg_params"]
        if isinstance(client_data.get("awg_params"), dict):
            try:
                return AmneziaWGParams(**client_data["awg_params"])
            except Exception:
                pass

        # 2. From client_data fields
        direct_client = {}
        for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
            val = client_data.get(k) if k in client_data else client_data.get(k.lower())
            if val is not None:
                try:
                    direct_client[k] = int(val)
                except (ValueError, TypeError):
                    pass
        if direct_client:
            try:
                return AmneziaWGParams(**direct_client)
            except Exception:
                pass

        # 3. From inbound settings
        settings = self._extract_settings_dict(inbound_data)
        awg_dict = settings.get("awg") or settings.get("amneziawg")
        if isinstance(awg_dict, dict) and awg_dict:
            parsed_awg = {}
            for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
                if k in awg_dict:
                    try:
                        parsed_awg[k] = int(awg_dict[k])
                    except (ValueError, TypeError):
                        pass
            if parsed_awg:
                try:
                    return AmneziaWGParams(**parsed_awg)
                except Exception:
                    pass

        # 4. Direct in settings or inbound_data
        inb_awg = {}
        for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
            val = settings.get(k) if k in settings else inbound_data.get(k)
            if val is not None:
                try:
                    inb_awg[k] = int(val)
                except (ValueError, TypeError):
                    pass
        if inb_awg:
            try:
                return AmneziaWGParams(**inb_awg)
            except Exception:
                pass

        # Default fallback
        return AmneziaWGParams()

    def generate_wireguard_conf(
        self,
        inbound_data: Dict[str, Any],
        client_data: Dict[str, Any],
        server_host: str,
    ) -> str:
        """
        Generate standard WireGuard client .conf file string.
        """
        port = inbound_data.get("port", 51820)
        endpoint = f"{server_host}:{port}"
        server_pub_key = self._extract_server_public_key(inbound_data)

        priv_key = client_data.get("private_key") or client_data.get("privateKey")
        if not priv_key:
            # Auto-generate a keypair if not pre-populated
            priv_key, _ = generate_wireguard_keypair()

        address = (
            client_data.get("address")
            or client_data.get("allowedIPs")
            or client_data.get("ip")
            or "10.66.66.2/32"
        )
        dns = client_data.get("dns", "1.1.1.1, 8.8.8.8")
        allowed_ips = client_data.get("allowed_ips", "0.0.0.0/0, ::/0")
        psk = client_data.get("preshared_key") or client_data.get("presharedKey")
        keepalive = client_data.get("persistent_keepalive", 25)
        mtu = client_data.get("mtu", 1420)

        return generate_wireguard_conf(
            private_key=priv_key,
            address=address,
            public_key=server_pub_key,
            endpoint=endpoint,
            allowed_ips=allowed_ips,
            dns=dns,
            mtu=mtu,
            preshared_key=psk,
            persistent_keepalive=keepalive,
        )

    def generate_awg_conf(
        self,
        inbound_data: Dict[str, Any],
        client_data: Dict[str, Any],
        server_host: str,
    ) -> str:
        """
        Generate AmneziaWG (AWG) client .conf file string with obfuscation headers.
        """
        port = inbound_data.get("port", 51820)
        endpoint = f"{server_host}:{port}"
        server_pub_key = self._extract_server_public_key(inbound_data)

        priv_key = client_data.get("private_key") or client_data.get("privateKey")
        if not priv_key:
            priv_key, _ = generate_wireguard_keypair()

        address = (
            client_data.get("address")
            or client_data.get("allowedIPs")
            or client_data.get("ip")
            or "10.66.66.2/32"
        )
        dns = client_data.get("dns", "1.1.1.1, 8.8.8.8")
        allowed_ips = client_data.get("allowed_ips", "0.0.0.0/0, ::/0")
        psk = client_data.get("preshared_key") or client_data.get("presharedKey")
        keepalive = client_data.get("persistent_keepalive", 25)
        mtu = client_data.get("mtu", 1360)
        awg_params = self._extract_awg_params(inbound_data, client_data)

        return generate_awg_conf(
            private_key=priv_key,
            address=address,
            public_key=server_pub_key,
            endpoint=endpoint,
            allowed_ips=allowed_ips,
            dns=dns,
            mtu=mtu,
            preshared_key=psk,
            persistent_keepalive=keepalive,
            awg_params=awg_params,
        )

    def generate_openvpn_conf(
        self,
        inbound_data: Dict[str, Any],
        client_data: Dict[str, Any],
        server_host: str,
    ) -> str:
        """
        Generate OpenVPN client .ovpn profile string.
        """
        if client_data.get("raw_ovpn"):
            return str(client_data["raw_ovpn"]).strip() + "\n"

        settings = self._extract_settings_dict(inbound_data)
        port = inbound_data.get("port", 1194)
        proto = inbound_data.get("proto") or settings.get("proto", "udp")
        dev = settings.get("dev", "tun")
        cipher = settings.get("cipher", "AES-256-GCM")
        auth = settings.get("auth", "SHA256")

        ca_cert = settings.get("ca_cert") or inbound_data.get("ca_cert")
        client_cert = client_data.get("client_cert") or settings.get("client_cert")
        client_key = client_data.get("client_key") or settings.get("client_key")
        tls_auth = settings.get("tls_auth") or inbound_data.get("tls_auth")
        tls_crypt = settings.get("tls_crypt") or inbound_data.get("tls_crypt")

        username = client_data.get("username") or client_data.get("email")
        password = client_data.get("password")

        profile = OpenVPNProfile(
            name=client_data.get("remark") or inbound_data.get("remark") or "VPN-Private",
            server_host=server_host,
            port=port,
            proto=proto,
            dev=dev,
            ca_cert=ca_cert,
            client_cert=client_cert,
            client_key=client_key,
            tls_auth=tls_auth,
            tls_crypt=tls_crypt,
            username=username,
            password=password,
            cipher=cipher,
            auth=auth,
        )
        return generate_openvpn_conf(profile)

    # -----------------------------------------------------------------------
    # Direct Client URL Generator (Extending 3x-ui standard links)
    # -----------------------------------------------------------------------

    def generate_client_url(
        self,
        protocol: str,
        inbound_data: Dict[str, Any],
        client_data: Dict[str, Any],
        server_host: str,
    ) -> str:
        """
        Generate direct protocol client URL or configuration link.
        Supports wireguard://, awg://, openvpn://, and falls back to super()
        for vless, vmess, trojan, and shadowsocks.
        """
        proto = protocol.strip().lower()

        if proto in (ProtocolType.WIREGUARD.value, "wg"):
            port = inbound_data.get("port", 51820)
            endpoint = f"{server_host}:{port}"
            server_pub = self._extract_server_public_key(inbound_data)
            priv_key = client_data.get("private_key") or client_data.get("privateKey")
            if not priv_key:
                priv_key, _ = generate_wireguard_keypair()
            address = client_data.get("address") or client_data.get("allowedIPs") or "10.66.66.2/32"
            dns = client_data.get("dns", "1.1.1.1")
            allowed_ips = client_data.get("allowed_ips", "0.0.0.0/0, ::/0")
            psk = client_data.get("preshared_key") or client_data.get("presharedKey")
            keepalive = client_data.get("persistent_keepalive", 25)
            mtu = client_data.get("mtu", 1420)
            remark = client_data.get("remark") or inbound_data.get("remark") or "WireGuard"

            return generate_wireguard_url(
                private_key=priv_key,
                endpoint=endpoint,
                public_key=server_pub,
                address=address,
                dns=dns,
                allowed_ips=allowed_ips,
                preshared_key=psk,
                keepalive=keepalive,
                mtu=mtu,
                remark=remark,
            )

        elif proto in (ProtocolType.AMNEZIAWG.value, "awg"):
            port = inbound_data.get("port", 51820)
            endpoint = f"{server_host}:{port}"
            server_pub = self._extract_server_public_key(inbound_data)
            priv_key = client_data.get("private_key") or client_data.get("privateKey")
            if not priv_key:
                priv_key, _ = generate_wireguard_keypair()
            address = client_data.get("address") or client_data.get("allowedIPs") or "10.66.66.2/32"
            dns = client_data.get("dns", "1.1.1.1")
            allowed_ips = client_data.get("allowed_ips", "0.0.0.0/0, ::/0")
            psk = client_data.get("preshared_key") or client_data.get("presharedKey")
            keepalive = client_data.get("persistent_keepalive", 25)
            mtu = client_data.get("mtu", 1360)
            awg_params = self._extract_awg_params(inbound_data, client_data)
            remark = client_data.get("remark") or inbound_data.get("remark") or "AmneziaWG"

            return generate_awg_url(
                private_key=priv_key,
                endpoint=endpoint,
                public_key=server_pub,
                address=address,
                dns=dns,
                allowed_ips=allowed_ips,
                preshared_key=psk,
                keepalive=keepalive,
                mtu=mtu,
                awg_params=awg_params,
                remark=remark,
            )

        elif proto in (ProtocolType.OPENVPN.value, "ovpn"):
            port = inbound_data.get("port", 1194)
            settings = self._extract_settings_dict(inbound_data)
            ovpn_proto = inbound_data.get("proto") or settings.get("proto", "udp")
            user = urllib.parse.quote(str(client_data.get("username") or client_data.get("email") or "user"))
            pwd = urllib.parse.quote(str(client_data.get("password") or ""))
            auth_part = f"{user}:{pwd}@" if pwd else f"{user}@"
            remark = urllib.parse.quote(str(client_data.get("remark") or inbound_data.get("remark") or "OpenVPN"))
            return f"openvpn://{auth_part}{server_host}:{port}?proto={ovpn_proto}#{remark}"

        # Delegate vless, vmess, trojan, shadowsocks to standard 3x-ui adapter
        return super().generate_client_url(protocol, inbound_data, client_data, server_host)

    # -----------------------------------------------------------------------
    # Typed Client Management
    # -----------------------------------------------------------------------

    async def add_wireguard_client(self, inbound_id: int, spec: WireGuardClientSpec) -> Dict[str, Any]:
        """Add a WireGuard peer using typed WireGuardClientSpec."""
        payload = spec.model_dump()
        return await self.vpn_client.add_wireguard_client(inbound_id, payload)

    async def add_amneziawg_client(self, inbound_id: int, spec: WireGuardClientSpec) -> Dict[str, Any]:
        """Add an AmneziaWG peer using typed WireGuardClientSpec."""
        payload = spec.model_dump()
        return await self.vpn_client.add_amneziawg_client(inbound_id, payload)

    async def add_openvpn_client(self, inbound_id: int, spec: OpenVPNClientSpec) -> Dict[str, Any]:
        """Add an OpenVPN client using typed OpenVPNClientSpec."""
        payload = spec.model_dump()
        return await self.vpn_client.add_openvpn_client(inbound_id, payload)
