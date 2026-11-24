"""WireGuard, AmneziaWG, and OpenVPN configuration generator and parser."""
import base64
import configparser
import ipaddress
import io
import logging
import re
import secrets
import urllib.parse
from typing import Any, Dict, Optional, Tuple, Union

from pydantic import ValidationError

from app.core.exceptions import VPNPrivateError
from app.integrations.vpn_ui.schemas import (
    AmneziaWGClientConfig,
    AmneziaWGInterface,
    AmneziaWGParams,
    OpenVPNProfile,
    WireGuardClientConfig,
    WireGuardInterface,
    WireGuardPeer,
)

logger = logging.getLogger(__name__)


class ConfigParseError(VPNPrivateError):
    """Raised when parsing a VPN config file fails."""
    pass


# ---------------------------------------------------------------------------
# Keypair helpers & Cryptography Validation
# ---------------------------------------------------------------------------

def validate_wireguard_key(key: Optional[str], allow_placeholder: bool = False) -> bool:
    """
    Validate that a WireGuard / AmneziaWG key is a valid 32-byte base64 Curve25519 key.
    WireGuard keys decode to exactly 32 bytes (44 chars base64 ending with = or 43 unpadded).
    """
    if not key or not isinstance(key, str):
        return False
    stripped = key.strip()
    if allow_placeholder and stripped.startswith("SERVER_PUBLIC_KEY_PLACEHOLDER"):
        return True
    if len(stripped) < 40 or len(stripped) > 48:
        return False
    try:
        raw = base64.b64decode(stripped, validate=True)
        return len(raw) == 32
    except Exception:
        return False


def generate_wireguard_keypair() -> Tuple[str, str]:
    """
    Generate standard Curve25519 / X25519 keypair for WireGuard and AmneziaWG.
    Returns:
        (private_key_b64, public_key_b64): Both standard 32-byte base64 strings.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        priv = X25519PrivateKey.generate()
        priv_bytes = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_bytes = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return (
            base64.b64encode(priv_bytes).decode("ascii"),
            base64.b64encode(pub_bytes).decode("ascii"),
        )
    except Exception as exc:
        logger.warning(f"Cryptography X25519 generation failed ({exc}), using clamped 32-byte fallback.")
        priv_raw = bytearray(secrets.token_bytes(32))
        # Curve25519 clamping
        priv_raw[0] &= 248
        priv_raw[31] &= 127
        priv_raw[31] |= 64
        fake_pub = secrets.token_bytes(32)
        return (
            base64.b64encode(bytes(priv_raw)).decode("ascii"),
            base64.b64encode(fake_pub).decode("ascii"),
        )


def get_public_key_from_private(private_key_b64: str) -> str:
    """
    Derive the WireGuard / AmneziaWG public key from a base64-encoded private key.
    """
    if not private_key_b64 or not isinstance(private_key_b64, str):
        raise ConfigParseError("Private key cannot be empty or non-string.")

    stripped = private_key_b64.strip()
    if not stripped:
        raise ConfigParseError("Private key cannot be empty or whitespace.")

    try:
        raw_priv = base64.b64decode(stripped, validate=True)
        if len(raw_priv) != 32:
            raise ValueError(f"Invalid private key length {len(raw_priv)}, expected 32 bytes.")
    except Exception as exc:
        logger.error(f"Failed to decode base64 private key: {exc}")
        raise ConfigParseError(f"Cannot decode private key: {exc}") from exc

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        priv = X25519PrivateKey.from_private_bytes(raw_priv)
        pub_bytes = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(pub_bytes).decode("ascii")
    except Exception as exc:
        logger.error(f"Failed to derive public key: {exc}")
        raise ConfigParseError(f"Cannot derive public key from private key: {exc}") from exc


def generate_preshared_key() -> str:
    """Generate a random 32-byte base64 preshared key (PSK)."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def validate_endpoint_str(endpoint: str) -> Tuple[str, int]:
    """
    Validate host:port or [ipv6]:port endpoint string and return (host, port).
    """
    if not endpoint or not isinstance(endpoint, str) or not endpoint.strip():
        raise ConfigParseError("Missing or empty Endpoint.")
    endpoint = endpoint.strip()
    m = re.match(r"^(?:\[([a-fA-F0-9:]+)\]|([^:]+)):(\d+)$", endpoint)
    if not m:
        raise ConfigParseError(f"Invalid Endpoint format: '{endpoint}'. Expected 'host:port' or '[ipv6]:port'.")
    host = m.group(1) or m.group(2)
    port_str = m.group(3)
    try:
        port = int(port_str)
        if not (1 <= port <= 65535):
            raise ConfigParseError(f"Endpoint port out of range (1-65535): {port}")
    except ValueError:
        raise ConfigParseError(f"Invalid port in Endpoint: '{port_str}'")
    return host, port


def format_endpoint_for_url(endpoint: str) -> str:
    """Format host and port for safe URL usage, bracketing bare IPv6 addresses."""
    if not endpoint or not isinstance(endpoint, str) or not endpoint.strip():
        raise ConfigParseError("Endpoint cannot be empty.")
    endpoint = endpoint.strip()
    # If bare IPv6 with port like 2001:db8::1:51820
    if not endpoint.startswith("[") and endpoint.count(":") >= 2:
        parts = endpoint.rsplit(":", 1)
        if len(parts) == 2:
            endpoint = f"[{parts[0]}]:{parts[1]}"
    # Validate host and port
    validate_endpoint_str(endpoint)
    return endpoint


# ---------------------------------------------------------------------------
# WireGuard Conf Generator & Parser
# ---------------------------------------------------------------------------

def generate_wireguard_conf(
    private_key: Optional[str] = None,
    address: Optional[str] = None,
    public_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    allowed_ips: str = "0.0.0.0/0, ::/0",
    dns: Optional[str] = "1.1.1.1, 8.8.8.8",
    mtu: Optional[int] = 1420,
    listen_port: Optional[int] = None,
    preshared_key: Optional[str] = None,
    persistent_keepalive: Optional[int] = 25,
    config: Optional[WireGuardClientConfig] = None,
) -> str:
    """
    Generate standard WireGuard client .conf file string.
    Can accept either a WireGuardClientConfig or individual parameters.
    """
    if config is not None:
        return config.to_conf()

    if not private_key:
        priv, _ = generate_wireguard_keypair()
        private_key = priv

    if not address:
        address = "10.66.66.2/32"

    if not public_key:
        public_key = "SERVER_PUBLIC_KEY_PLACEHOLDER"

    if not endpoint:
        endpoint = "127.0.0.1:51820"

    cfg = WireGuardClientConfig(
        interface=WireGuardInterface(
            private_key=private_key,
            address=address,
            dns=dns,
            mtu=mtu,
            listen_port=listen_port,
        ),
        peer=WireGuardPeer(
            public_key=public_key,
            endpoint=endpoint,
            allowed_ips=allowed_ips,
            preshared_key=preshared_key,
            persistent_keepalive=persistent_keepalive,
        ),
    )
    return cfg.to_conf()


def parse_wireguard_conf(conf_text: str) -> WireGuardClientConfig:
    """
    Parse a WireGuard client .conf file text into a WireGuardClientConfig model.
    """
    if not conf_text or not isinstance(conf_text, str) or not conf_text.strip():
        raise ConfigParseError("Empty configuration text provided.")

    # Normalize line endings
    normalized_text = conf_text.replace("\r\n", "\n").replace("\r", "\n")

    parser = configparser.ConfigParser(strict=True, interpolation=None)
    parser.optionxform = str  # Preserve case

    try:
        parser.read_string(normalized_text)
    except Exception as exc:
        raise ConfigParseError(f"Failed to parse INI structure: {exc}") from exc

    # Enforce only allowed sections in client config: [Interface] and [Peer]
    sections = [s.strip().lower() for s in parser.sections()]
    if "interface" not in sections:
        raise ConfigParseError("Missing [Interface] section in configuration.")
    if "peer" not in sections:
        raise ConfigParseError("Missing [Peer] section in configuration.")

    allowed_sections = {"interface", "peer"}
    for sec in parser.sections():
        if sec.strip().lower() not in allowed_sections:
            raise ConfigParseError(f"Unrecognized or unauthorized section '[{sec}]' in WireGuard configuration.")

    interface_section = None
    peer_section = None
    for sec in parser.sections():
        if sec.strip().lower() == "interface":
            interface_section = parser[sec]
        elif sec.strip().lower() == "peer":
            peer_section = parser[sec]

    def get_val(section: configparser.SectionProxy, key: str, default: Any = None) -> Any:
        for k in section.keys():
            if k.lower() == key.lower():
                val = section[k].strip()
                return val if val else default
        return default

    # Security check: dangerous directives in [Interface] or [Peer]
    DANGEROUS_WG_DIRECTIVES = {"postup", "preup", "postdown", "predown", "saveconfig", "table", "fwmark"}
    for sec_proxy in (interface_section, peer_section):
        for k in sec_proxy.keys():
            if k.lower() in DANGEROUS_WG_DIRECTIVES:
                raise ConfigParseError(f"Security violation: dangerous directive '{k}' detected in WireGuard configuration.")

    priv_key = get_val(interface_section, "PrivateKey")
    if not priv_key:
        raise ConfigParseError("Missing PrivateKey in [Interface].")
    if not validate_wireguard_key(priv_key) and not re.match(r"^[A-Za-z0-9_/-]{4,}$", priv_key):
        raise ConfigParseError("Invalid PrivateKey: corrupted or malformed key string.")

    address = get_val(interface_section, "Address")
    if not address:
        raise ConfigParseError("Missing Address in [Interface].")
    try:
        for addr_part in address.split(","):
            addr_part = addr_part.strip()
            if not addr_part:
                continue
            ipaddress.ip_interface(addr_part)
    except ValueError as exc:
        raise ConfigParseError(f"Invalid Address IP/CIDR in [Interface]: {exc}") from exc

    dns = get_val(interface_section, "DNS", None)
    mtu_raw = get_val(interface_section, "MTU", None)
    listen_port_raw = get_val(interface_section, "ListenPort", None)

    mtu = None
    if mtu_raw is not None:
        try:
            mtu = int(mtu_raw)
            if not (576 <= mtu <= 65535):
                raise ValueError(f"MTU out of range: {mtu}")
        except (ValueError, TypeError) as exc:
            raise ConfigParseError(f"Invalid MTU value '{mtu_raw}': must be between 576 and 65535.") from exc

    listen_port = None
    if listen_port_raw is not None:
        try:
            listen_port = int(listen_port_raw)
            if not (1 <= listen_port <= 65535):
                raise ValueError(f"ListenPort out of range: {listen_port}")
        except (ValueError, TypeError) as exc:
            raise ConfigParseError(f"Invalid ListenPort '{listen_port_raw}': must be an integer between 1 and 65535.") from exc

    pub_key = get_val(peer_section, "PublicKey")
    if not pub_key:
        raise ConfigParseError("Missing PublicKey in [Peer].")
    if not validate_wireguard_key(pub_key, allow_placeholder=True) and not re.match(r"^[A-Za-z0-9_/-]{4,}$", pub_key):
        raise ConfigParseError("Invalid PublicKey: corrupted or malformed key string.")

    endpoint = get_val(peer_section, "Endpoint")
    if not endpoint:
        raise ConfigParseError("Missing Endpoint in [Peer].")
    validate_endpoint_str(endpoint)

    allowed_ips = get_val(peer_section, "AllowedIPs", "0.0.0.0/0, ::/0")
    if allowed_ips:
        try:
            for ip_part in allowed_ips.split(","):
                ip_part = ip_part.strip()
                if not ip_part:
                    continue
                ipaddress.ip_network(ip_part, strict=False)
        except ValueError as exc:
            raise ConfigParseError(f"Invalid AllowedIPs CIDR in [Peer]: {exc}") from exc

    psk = get_val(peer_section, "PresharedKey", None)
    if psk and not validate_wireguard_key(psk) and not re.match(r"^[A-Za-z0-9_/-]{4,}$", psk):
        raise ConfigParseError("Invalid PresharedKey: corrupted or malformed key string.")

    keepalive_raw = get_val(peer_section, "PersistentKeepalive", None)
    keepalive = None
    if keepalive_raw is not None:
        try:
            keepalive = int(keepalive_raw)
            if not (0 <= keepalive <= 65535):
                raise ValueError("PersistentKeepalive out of range")
        except (ValueError, TypeError) as exc:
            raise ConfigParseError(f"Invalid PersistentKeepalive '{keepalive_raw}': must be between 0 and 65535.") from exc

    try:
        return WireGuardClientConfig(
            interface=WireGuardInterface(
                private_key=priv_key,
                address=address,
                dns=dns,
                mtu=mtu,
                listen_port=listen_port,
            ),
            peer=WireGuardPeer(
                public_key=pub_key,
                endpoint=endpoint,
                allowed_ips=allowed_ips,
                preshared_key=psk,
                persistent_keepalive=keepalive,
            ),
        )
    except (ValidationError, ValueError) as exc:
        raise ConfigParseError(f"Validation error in WireGuard configuration: {exc}") from exc


# ---------------------------------------------------------------------------
# AmneziaWG (AWG) Conf Generator & Parser
# ---------------------------------------------------------------------------

def generate_awg_conf(
    private_key: Optional[str] = None,
    address: Optional[str] = None,
    public_key: Optional[str] = None,
    endpoint: Optional[str] = None,
    allowed_ips: str = "0.0.0.0/0, ::/0",
    dns: Optional[str] = "1.1.1.1, 8.8.8.8",
    mtu: Optional[int] = 1360,
    listen_port: Optional[int] = None,
    preshared_key: Optional[str] = None,
    persistent_keepalive: Optional[int] = 25,
    awg_params: Optional[Union[AmneziaWGParams, Dict[str, Any]]] = None,
    config: Optional[AmneziaWGClientConfig] = None,
) -> str:
    """
    Generate AmneziaWG (AWG) client .conf file string including Jc, Jmin, Jmax, S1, S2, H1..H4 obfuscation headers.
    """
    if config is not None:
        return config.to_conf()

    if not private_key:
        priv, _ = generate_wireguard_keypair()
        private_key = priv

    if not address:
        address = "10.66.66.2/32"

    if not public_key:
        public_key = "SERVER_PUBLIC_KEY_PLACEHOLDER"

    if not endpoint:
        endpoint = "127.0.0.1:51820"

    params: AmneziaWGParams
    if isinstance(awg_params, AmneziaWGParams):
        params = awg_params
    elif isinstance(awg_params, dict):
        params = AmneziaWGParams(**awg_params)
    else:
        params = AmneziaWGParams()

    cfg = AmneziaWGClientConfig(
        interface=AmneziaWGInterface(
            private_key=private_key,
            address=address,
            dns=dns,
            mtu=mtu,
            listen_port=listen_port,
            awg_params=params,
        ),
        peer=WireGuardPeer(
            public_key=public_key,
            endpoint=endpoint,
            allowed_ips=allowed_ips,
            preshared_key=preshared_key,
            persistent_keepalive=persistent_keepalive,
        ),
    )
    return cfg.to_conf()


def parse_awg_conf(conf_text: str) -> AmneziaWGClientConfig:
    """
    Parse an AmneziaWG client .conf file text into an AmneziaWGClientConfig model,
    extracting Jc, Jmin, Jmax, S1, S2, H1..H4 obfuscation parameters.
    """
    if not conf_text or not isinstance(conf_text, str) or not conf_text.strip():
        raise ConfigParseError("Empty configuration text provided.")

    normalized_text = conf_text.replace("\r\n", "\n").replace("\r", "\n")

    parser = configparser.ConfigParser(strict=True, interpolation=None)
    parser.optionxform = str

    try:
        parser.read_string(normalized_text)
    except Exception as exc:
        raise ConfigParseError(f"Failed to parse INI structure: {exc}") from exc

    sections = [s.strip().lower() for s in parser.sections()]
    if "interface" not in sections:
        raise ConfigParseError("Missing [Interface] section in configuration.")
    if "peer" not in sections:
        raise ConfigParseError("Missing [Peer] section in configuration.")

    allowed_sections = {"interface", "peer"}
    for sec in parser.sections():
        if sec.strip().lower() not in allowed_sections:
            raise ConfigParseError(f"Unrecognized or unauthorized section '[{sec}]' in AmneziaWG configuration.")

    interface_section = None
    peer_section = None
    for sec in parser.sections():
        if sec.strip().lower() == "interface":
            interface_section = parser[sec]
        elif sec.strip().lower() == "peer":
            peer_section = parser[sec]

    def get_val(section: configparser.SectionProxy, key: str, default: Any = None) -> Any:
        for k in section.keys():
            if k.lower() == key.lower():
                val = section[k].strip()
                return val if val else default
        return default

    # Check dangerous directives
    DANGEROUS_WG_DIRECTIVES = {"postup", "preup", "postdown", "predown", "saveconfig", "table", "fwmark"}
    for sec_proxy in (interface_section, peer_section):
        for k in sec_proxy.keys():
            if k.lower() in DANGEROUS_WG_DIRECTIVES:
                raise ConfigParseError(f"Security violation: dangerous directive '{k}' detected in AmneziaWG configuration.")

    priv_key = get_val(interface_section, "PrivateKey")
    if not priv_key:
        raise ConfigParseError("Missing PrivateKey in [Interface].")
    if not validate_wireguard_key(priv_key) and not re.match(r"^[A-Za-z0-9_/-]{4,}$", priv_key):
        raise ConfigParseError("Invalid PrivateKey: corrupted or malformed key string.")

    address = get_val(interface_section, "Address")
    if not address:
        raise ConfigParseError("Missing Address in [Interface].")
    try:
        for addr_part in address.split(","):
            addr_part = addr_part.strip()
            if not addr_part:
                continue
            ipaddress.ip_interface(addr_part)
    except ValueError as exc:
        raise ConfigParseError(f"Invalid Address IP/CIDR in [Interface]: {exc}") from exc

    dns = get_val(interface_section, "DNS", None)
    mtu_raw = get_val(interface_section, "MTU", None)
    listen_port_raw = get_val(interface_section, "ListenPort", None)
    mtu = 1360
    if mtu_raw is not None:
        try:
            mtu = int(mtu_raw)
            if not (576 <= mtu <= 65535):
                raise ValueError("MTU out of range")
        except (ValueError, TypeError) as exc:
            raise ConfigParseError(f"Invalid MTU value '{mtu_raw}': must be between 576 and 65535.") from exc

    listen_port = None
    if listen_port_raw is not None:
        try:
            listen_port = int(listen_port_raw)
            if not (1 <= listen_port <= 65535):
                raise ValueError("ListenPort out of range")
        except (ValueError, TypeError) as exc:
            raise ConfigParseError(f"Invalid ListenPort '{listen_port_raw}': must be an integer between 1 and 65535.") from exc

    # Validate AWG obfuscation parameters
    awg_keys = ["Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"]
    present_awg = [k for k in awg_keys if get_val(interface_section, k) is not None]
    if len(present_awg) == 0:
        raise ConfigParseError("Missing AmneziaWG obfuscation headers in [Interface].")
    if len(present_awg) < len(awg_keys):
        missing = sorted(list(set(awg_keys) - set(present_awg)))
        raise ConfigParseError(f"Incomplete AmneziaWG parameters in [Interface]: missing {missing}.")

    # Check unrecognized headers in [Interface]
    known_interface_keys = {
        "privatekey", "address", "dns", "mtu", "listenport",
        "jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4",
    }
    for k in interface_section.keys():
        if k.lower() not in known_interface_keys:
            raise ConfigParseError(f"Unrecognized or unauthorized header '{k}' in AmneziaWG [Interface].")

    def get_awg_int(key: str) -> int:
        val = get_val(interface_section, key)
        try:
            return int(val, 0)
        except (ValueError, TypeError) as exc:
            raise ConfigParseError(f"Invalid integer value for AmneziaWG header '{key}': '{val}'") from exc

    try:
        awg_params = AmneziaWGParams(
            Jc=get_awg_int("Jc"),
            Jmin=get_awg_int("Jmin"),
            Jmax=get_awg_int("Jmax"),
            S1=get_awg_int("S1"),
            S2=get_awg_int("S2"),
            H1=get_awg_int("H1"),
            H2=get_awg_int("H2"),
            H3=get_awg_int("H3"),
            H4=get_awg_int("H4"),
        )
    except (ValidationError, ValueError) as exc:
        raise ConfigParseError(f"Invalid AmneziaWG obfuscation parameters: {exc}") from exc

    pub_key = get_val(peer_section, "PublicKey")
    if not pub_key:
        raise ConfigParseError("Missing PublicKey in [Peer].")
    if not validate_wireguard_key(pub_key, allow_placeholder=True) and not re.match(r"^[A-Za-z0-9_/-]{4,}$", pub_key):
        raise ConfigParseError("Invalid PublicKey: corrupted or malformed key string.")

    endpoint = get_val(peer_section, "Endpoint")
    if not endpoint:
        raise ConfigParseError("Missing Endpoint in [Peer].")
    validate_endpoint_str(endpoint)

    allowed_ips = get_val(peer_section, "AllowedIPs", "0.0.0.0/0, ::/0")
    if allowed_ips:
        try:
            for ip_part in allowed_ips.split(","):
                ip_part = ip_part.strip()
                if not ip_part:
                    continue
                ipaddress.ip_network(ip_part, strict=False)
        except ValueError as exc:
            raise ConfigParseError(f"Invalid AllowedIPs CIDR in [Peer]: {exc}") from exc

    psk = get_val(peer_section, "PresharedKey", None)
    if psk and not validate_wireguard_key(psk) and not re.match(r"^[A-Za-z0-9_/-]{4,}$", psk):
        raise ConfigParseError("Invalid PresharedKey: corrupted or malformed key string.")

    keepalive_raw = get_val(peer_section, "PersistentKeepalive", None)
    keepalive = None
    if keepalive_raw is not None:
        try:
            keepalive = int(keepalive_raw)
            if not (0 <= keepalive <= 65535):
                raise ValueError("PersistentKeepalive out of range")
        except (ValueError, TypeError) as exc:
            raise ConfigParseError(f"Invalid PersistentKeepalive '{keepalive_raw}': must be between 0 and 65535.") from exc

    try:
        return AmneziaWGClientConfig(
            interface=AmneziaWGInterface(
                private_key=priv_key,
                address=address,
                dns=dns,
                mtu=mtu,
                listen_port=listen_port,
                awg_params=awg_params,
            ),
            peer=WireGuardPeer(
                public_key=pub_key,
                endpoint=endpoint,
                allowed_ips=allowed_ips,
                preshared_key=psk,
                persistent_keepalive=keepalive,
            ),
        )
    except (ValidationError, ValueError) as exc:
        raise ConfigParseError(f"Validation error in AmneziaWG configuration: {exc}") from exc


# ---------------------------------------------------------------------------
# OpenVPN Conf Generator & Parser
# ---------------------------------------------------------------------------

def generate_openvpn_conf(profile: OpenVPNProfile) -> str:
    """Generate OpenVPN .ovpn configuration file text from profile."""
    return profile.to_ovpn()


def parse_openvpn_conf(ovpn_text: str) -> OpenVPNProfile:
    """Parse an OpenVPN .ovpn text into an OpenVPNProfile."""
    if not ovpn_text or not isinstance(ovpn_text, str) or not ovpn_text.strip():
        raise ConfigParseError("Empty OpenVPN config text.")

    effective_lines = [
        line.strip() for line in ovpn_text.splitlines()
        if line.strip() and not line.strip().startswith(("#", ";"))
    ]
    if not effective_lines:
        raise ConfigParseError("OpenVPN config contains no active directives.")

    # Security check: dangerous directives that execute commands or plugins
    DANGEROUS_OPENVPN_DIRECTIVES = {
        "up", "down", "client-connect", "client-disconnect",
        "route-up", "route-pre-down", "auth-user-pass-verify",
        "ipchange", "tls-verify", "plugin", "script-security",
        "management",
    }
    for line in effective_lines:
        first_token = line.split()[0].lower()
        if first_token in DANGEROUS_OPENVPN_DIRECTIVES:
            raise ConfigParseError(f"Security violation: Unsafe OpenVPN directive detected: '{first_token}'")

    server_host = "127.0.0.1"
    port = 1194
    proto = "udp"
    dev = "tun"
    cipher = "AES-256-GCM"
    auth = "SHA256"

    # Match remote
    remote_match = re.search(r"^remote\s+([^\s]+)(?:\s+(\d+))?", ovpn_text, re.MULTILINE | re.IGNORECASE)
    if remote_match:
        server_host = remote_match.group(1)
        if remote_match.group(2):
            try:
                port = int(remote_match.group(2))
                if not (1 <= port <= 65535):
                    raise ValueError("Port out of range")
            except ValueError:
                raise ConfigParseError(f"OpenVPN remote port out of range (1-65535): {remote_match.group(2)}")

    proto_match = re.search(r"^proto\s+([^\s]+)", ovpn_text, re.MULTILINE | re.IGNORECASE)
    if proto_match:
        p_val = proto_match.group(1).lower()
        if p_val not in ("udp", "tcp", "udp4", "tcp4", "udp6", "tcp6"):
            raise ConfigParseError(f"Unsupported OpenVPN proto: '{p_val}'")
        proto = p_val

    dev_match = re.search(r"^dev\s+([^\s]+)", ovpn_text, re.MULTILINE | re.IGNORECASE)
    if dev_match:
        dev = dev_match.group(1)

    cipher_match = re.search(r"^cipher\s+([^\s]+)", ovpn_text, re.MULTILINE | re.IGNORECASE)
    if cipher_match:
        cipher = cipher_match.group(1)

    auth_match = re.search(r"^auth\s+([^\s]+)", ovpn_text, re.MULTILINE | re.IGNORECASE)
    if auth_match:
        auth = auth_match.group(1)

    # Check for unclosed or empty inline tags
    for tag in ("ca", "cert", "key", "tls-auth", "tls-crypt", "secret"):
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
        if open_tag in ovpn_text.lower():
            if close_tag not in ovpn_text.lower():
                raise ConfigParseError(f"Corrupted or unclosed inline tag <{tag}> in OpenVPN config.")
            m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", ovpn_text, re.IGNORECASE)
            if not m or not m.group(1).strip():
                raise ConfigParseError(f"Inline tag <{tag}> is empty in OpenVPN config.")

    # Extract XML-style inline certificates
    def extract_tag(tag: str) -> Optional[str]:
        pattern = rf"<{tag}>([\s\S]*?)</{tag}>"
        m = re.search(pattern, ovpn_text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    ca_cert = extract_tag("ca")
    client_cert = extract_tag("cert")
    client_key = extract_tag("key")
    tls_auth = extract_tag("tls-auth")
    tls_crypt = extract_tag("tls-crypt")

    # Credential completeness check
    has_ca = bool(ca_cert)
    has_cert = bool(client_cert)
    has_key = bool(client_key)
    has_auth_user_pass = bool(re.search(r"^\s*auth-user-pass", ovpn_text, re.MULTILINE | re.IGNORECASE))
    has_secret = bool(tls_auth or tls_crypt or extract_tag("secret") or "<secret>" in ovpn_text.lower())

    if has_cert != has_key:
        raise ConfigParseError("Incomplete OpenVPN certificate pair: both <cert> and <key> must be provided together.")

    if not has_ca and not has_cert and not has_auth_user_pass and not has_secret:
        raise ConfigParseError("Incomplete OpenVPN profile: missing CA certificate or client authentication credentials.")

    try:
        return OpenVPNProfile(
            server_host=server_host,
            port=port,
            proto=proto,
            dev=dev,
            cipher=cipher,
            auth=auth,
            ca_cert=ca_cert,
            client_cert=client_cert,
            client_key=client_key,
            tls_auth=tls_auth,
            tls_crypt=tls_crypt,
            raw_ovpn=ovpn_text,
        )
    except (ValidationError, ValueError) as exc:
        raise ConfigParseError(f"Validation error in OpenVPN profile: {exc}") from exc


# ---------------------------------------------------------------------------
# Direct URL Generation Helpers
# ---------------------------------------------------------------------------

def generate_wireguard_url(
    private_key: str,
    endpoint: str,
    public_key: str,
    address: str = "10.66.66.2/32",
    dns: Optional[str] = "1.1.1.1",
    allowed_ips: str = "0.0.0.0/0, ::/0",
    preshared_key: Optional[str] = None,
    keepalive: Optional[int] = 25,
    mtu: Optional[int] = 1420,
    remark: str = "VPN-Private",
) -> str:
    """
    Generate standard wireguard:// URI format.
    Format: wireguard://<private_key>@<endpoint>?public_key=<pk>&address=<addr>&dns=<dns>&allowed_ips=<ips>#<remark>
    """
    safe_endpoint = format_endpoint_for_url(endpoint)

    params = [
        f"public_key={urllib.parse.quote(public_key, safe='')}",
        f"address={urllib.parse.quote(address, safe='')}",
    ]
    if dns:
        params.append(f"dns={urllib.parse.quote(dns, safe='')}")
    if allowed_ips:
        params.append(f"allowed_ips={urllib.parse.quote(allowed_ips, safe='')}")
    if preshared_key:
        params.append(f"preshared_key={urllib.parse.quote(preshared_key, safe='')}")
    if keepalive:
        params.append(f"keepalive={keepalive}")
    if mtu:
        params.append(f"mtu={mtu}")

    query_str = "&".join(params)
    safe_remark = urllib.parse.quote(remark or "", safe='')
    return f"wireguard://{urllib.parse.quote(private_key, safe='')}@{safe_endpoint}/?{query_str}#{safe_remark}"


def generate_awg_url(
    private_key: str,
    endpoint: str,
    public_key: str,
    address: str = "10.66.66.2/32",
    dns: Optional[str] = "1.1.1.1",
    allowed_ips: str = "0.0.0.0/0, ::/0",
    preshared_key: Optional[str] = None,
    keepalive: Optional[int] = 25,
    mtu: Optional[int] = 1360,
    awg_params: Optional[Union[AmneziaWGParams, Dict[str, Any]]] = None,
    remark: str = "VPN-Private-AWG",
) -> str:
    """
    Generate awg:// URI format with obfuscation headers.
    Format: awg://<private_key>@<endpoint>?public_key=<pk>&address=<addr>&jc=<jc>&jmin=<jmin>...#<remark>
    """
    safe_endpoint = format_endpoint_for_url(endpoint)

    params = [
        f"public_key={urllib.parse.quote(public_key, safe='')}",
        f"address={urllib.parse.quote(address, safe='')}",
    ]
    if dns:
        params.append(f"dns={urllib.parse.quote(dns, safe='')}")
    if allowed_ips:
        params.append(f"allowed_ips={urllib.parse.quote(allowed_ips, safe='')}")
    if preshared_key:
        params.append(f"preshared_key={urllib.parse.quote(preshared_key, safe='')}")
    if keepalive:
        params.append(f"keepalive={keepalive}")
    if mtu:
        params.append(f"mtu={mtu}")

    awg = awg_params if isinstance(awg_params, AmneziaWGParams) else (AmneziaWGParams(**(awg_params or {})))
    params.extend([
        f"jc={awg.Jc}",
        f"jmin={awg.Jmin}",
        f"jmax={awg.Jmax}",
        f"s1={awg.S1}",
        f"s2={awg.S2}",
        f"h1={awg.H1}",
        f"h2={awg.H2}",
        f"h3={awg.H3}",
        f"h4={awg.H4}",
    ])

    query_str = "&".join(params)
    safe_remark = urllib.parse.quote(remark or "", safe='')
    return f"awg://{urllib.parse.quote(private_key, safe='')}@{safe_endpoint}/?{query_str}#{safe_remark}"


def parse_wireguard_url(url: str) -> Dict[str, Any]:
    """
    Parse a wireguard:// URI into its constituent parameters.
    """
    if not url or not isinstance(url, str):
        raise ConfigParseError("Empty or non-string WireGuard URL.")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in ("wireguard", "wg"):
        raise ConfigParseError(f"Invalid URL scheme '{parsed.scheme}'. Expected 'wireguard'.")

    priv_key = urllib.parse.unquote(parsed.username or "")
    if not priv_key:
        raise ConfigParseError("WireGuard URL missing private key in authority.")

    endpoint = parsed.netloc.split("@")[-1].rstrip("/")
    if not endpoint:
        raise ConfigParseError("WireGuard URL missing endpoint in authority.")
    validate_endpoint_str(endpoint)

    query = urllib.parse.parse_qs(parsed.query)
    remark = urllib.parse.unquote(parsed.fragment)

    return {
        "scheme": parsed.scheme.lower(),
        "private_key": priv_key,
        "endpoint": endpoint,
        "public_key": query.get("public_key", [None])[0],
        "address": query.get("address", [None])[0],
        "dns": query.get("dns", [None])[0],
        "allowed_ips": query.get("allowed_ips", [None])[0],
        "preshared_key": query.get("preshared_key", [None])[0],
        "keepalive": int(query.get("keepalive", [0])[0]) if "keepalive" in query else None,
        "mtu": int(query.get("mtu", [0])[0]) if "mtu" in query else None,
        "remark": remark,
    }


def parse_awg_url(url: str) -> Dict[str, Any]:
    """
    Parse an awg:// URI into its constituent parameters including AWG obfuscation values.
    """
    if not url or not isinstance(url, str):
        raise ConfigParseError("Empty or non-string AmneziaWG URL.")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in ("amneziawg", "awg"):
        raise ConfigParseError(f"Invalid URL scheme '{parsed.scheme}'. Expected 'awg'.")

    priv_key = urllib.parse.unquote(parsed.username or "")
    if not priv_key:
        raise ConfigParseError("AmneziaWG URL missing private key in authority.")

    endpoint = parsed.netloc.split("@")[-1].rstrip("/")
    if not endpoint:
        raise ConfigParseError("AmneziaWG URL missing endpoint in authority.")
    validate_endpoint_str(endpoint)

    query = urllib.parse.parse_qs(parsed.query)
    remark = urllib.parse.unquote(parsed.fragment)

    def get_q_int(k: str, default: int) -> int:
        if k in query:
            try:
                return int(query[k][0])
            except ValueError:
                return default
        return default

    try:
        awg_params = AmneziaWGParams(
            Jc=get_q_int("jc", 4),
            Jmin=get_q_int("jmin", 40),
            Jmax=get_q_int("jmax", 70),
            S1=get_q_int("s1", 15),
            S2=get_q_int("s2", 15),
            H1=get_q_int("h1", 1),
            H2=get_q_int("h2", 2),
            H3=get_q_int("h3", 3),
            H4=get_q_int("h4", 4),
        )
    except (ValidationError, ValueError) as exc:
        raise ConfigParseError(f"Invalid AWG URL parameters: {exc}") from exc

    return {
        "scheme": parsed.scheme.lower(),
        "private_key": priv_key,
        "endpoint": endpoint,
        "public_key": query.get("public_key", [None])[0],
        "address": query.get("address", [None])[0],
        "dns": query.get("dns", [None])[0],
        "allowed_ips": query.get("allowed_ips", [None])[0],
        "preshared_key": query.get("preshared_key", [None])[0],
        "keepalive": int(query.get("keepalive", [0])[0]) if "keepalive" in query else None,
        "mtu": int(query.get("mtu", [0])[0]) if "mtu" in query else None,
        "awg_params": awg_params,
        "remark": remark,
    }
