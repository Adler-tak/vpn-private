"""Pydantic schemas for VPN-UI (WireGuard, AmneziaWG, and OpenVPN)."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class AmneziaWGParams(BaseModel):
    """AmneziaWG (AWG) header obfuscation and junk packet parameters."""
    Jc: int = Field(default=4, ge=1, le=128, description="Junk packet count")
    Jmin: int = Field(default=40, ge=0, le=65535, description="Minimum junk packet size in bytes")
    Jmax: int = Field(default=70, ge=0, le=65535, description="Maximum junk packet size in bytes")
    S1: int = Field(default=15, ge=0, le=65535, description="Init packet junk size in bytes")
    S2: int = Field(default=15, ge=0, le=65535, description="Response packet junk size in bytes")
    H1: int = Field(default=1, ge=0, le=4294967295, description="Init packet magic header (32-bit int)")
    H2: int = Field(default=2, ge=0, le=4294967295, description="Response packet magic header (32-bit int)")
    H3: int = Field(default=3, ge=0, le=4294967295, description="Underload packet magic header (32-bit int)")
    H4: int = Field(default=4, ge=0, le=4294967295, description="Transport data packet magic header (32-bit int)")

    @model_validator(mode="after")
    def validate_bounds(self) -> "AmneziaWGParams":
        if self.Jmin > self.Jmax:
            raise ValueError(f"Jmin ({self.Jmin}) cannot be greater than Jmax ({self.Jmax})")
        if len({self.H1, self.H2, self.H3, self.H4}) != 4:
            raise ValueError("Magic headers H1, H2, H3, and H4 must all be distinct values.")
        return self

    def to_dict(self) -> Dict[str, int]:
        return {
            "Jc": self.Jc,
            "Jmin": self.Jmin,
            "Jmax": self.Jmax,
            "S1": self.S1,
            "S2": self.S2,
            "H1": self.H1,
            "H2": self.H2,
            "H3": self.H3,
            "H4": self.H4,
        }

    def to_conf_lines(self) -> List[str]:
        return [
            f"Jc = {self.Jc}",
            f"Jmin = {self.Jmin}",
            f"Jmax = {self.Jmax}",
            f"S1 = {self.S1}",
            f"S2 = {self.S2}",
            f"H1 = {self.H1}",
            f"H2 = {self.H2}",
            f"H3 = {self.H3}",
            f"H4 = {self.H4}",
        ]


class WireGuardInterface(BaseModel):
    """WireGuard client interface configuration."""
    private_key: str = Field(description="Base64 encoded Curve25519 private key")
    address: str = Field(description="Client IP address with subnet CIDR, e.g. 10.66.66.2/32")
    dns: Optional[str] = Field(default="1.1.1.1, 8.8.8.8", description="DNS servers comma-separated")
    mtu: Optional[int] = Field(default=1420, ge=576, le=65535, description="Maximum Transmission Unit")
    listen_port: Optional[int] = Field(default=None, ge=1, le=65535, description="Optional local listening port")


class AmneziaWGInterface(WireGuardInterface):
    """AmneziaWG client interface configuration with obfuscation parameters."""
    mtu: Optional[int] = Field(default=1360, ge=576, le=65535, description="AWG MTU (recommended 1360 for junk padding)")
    awg_params: Optional[AmneziaWGParams] = Field(default_factory=AmneziaWGParams)

    # Direct field access for AWG params
    Jc: Optional[int] = None
    Jmin: Optional[int] = None
    Jmax: Optional[int] = None
    S1: Optional[int] = None
    S2: Optional[int] = None
    H1: Optional[int] = None
    H2: Optional[int] = None
    H3: Optional[int] = None
    H4: Optional[int] = None

    @model_validator(mode="after")
    def sync_awg_params(self) -> "AmneziaWGInterface":
        params = self.awg_params or AmneziaWGParams()
        override: Dict[str, Any] = {}
        for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
            val = getattr(self, k, None)
            if val is not None:
                override[k] = val
        if override:
            self.awg_params = AmneziaWGParams(**{**params.to_dict(), **override})
        else:
            self.awg_params = params
        return self


class WireGuardPeer(BaseModel):
    """WireGuard/AmneziaWG peer (server) endpoint configuration."""
    public_key: str = Field(description="Base64 encoded Curve25519 server public key")
    endpoint: str = Field(description="Server host/IP and port e.g. vpn.example.com:51820")
    allowed_ips: str = Field(default="0.0.0.0/0, ::/0", description="Routable CIDR ranges")
    preshared_key: Optional[str] = Field(default=None, description="Optional base64 preshared key")
    persistent_keepalive: Optional[int] = Field(default=25, ge=0, le=65535, description="Keepalive interval in seconds")


class WireGuardClientConfig(BaseModel):
    """Full WireGuard client configuration holding interface and peer."""
    interface: WireGuardInterface
    peer: WireGuardPeer

    def to_conf(self) -> str:
        lines = [
            "[Interface]",
            f"PrivateKey = {self.interface.private_key}",
            f"Address = {self.interface.address}",
        ]
        if self.interface.dns:
            lines.append(f"DNS = {self.interface.dns}")
        if self.interface.mtu:
            lines.append(f"MTU = {self.interface.mtu}")
        if self.interface.listen_port:
            lines.append(f"ListenPort = {self.interface.listen_port}")

        lines.extend([
            "",
            "[Peer]",
            f"PublicKey = {self.peer.public_key}",
        ])
        if self.peer.preshared_key:
            lines.append(f"PresharedKey = {self.peer.preshared_key}")
        lines.append(f"Endpoint = {self.peer.endpoint}")
        lines.append(f"AllowedIPs = {self.peer.allowed_ips}")
        if self.peer.persistent_keepalive:
            lines.append(f"PersistentKeepalive = {self.peer.persistent_keepalive}")

        return "\n".join(lines) + "\n"


class AmneziaWGClientConfig(BaseModel):
    """Full AmneziaWG client configuration holding AWG interface and peer."""
    interface: AmneziaWGInterface
    peer: WireGuardPeer

    def to_conf(self) -> str:
        lines = [
            "[Interface]",
            f"PrivateKey = {self.interface.private_key}",
            f"Address = {self.interface.address}",
        ]
        if self.interface.dns:
            lines.append(f"DNS = {self.interface.dns}")
        if self.interface.mtu:
            lines.append(f"MTU = {self.interface.mtu}")
        if self.interface.listen_port:
            lines.append(f"ListenPort = {self.interface.listen_port}")

        # AWG obfuscation parameters in [Interface]
        params = self.interface.awg_params or AmneziaWGParams()
        lines.extend(params.to_conf_lines())

        lines.extend([
            "",
            "[Peer]",
            f"PublicKey = {self.peer.public_key}",
        ])
        if self.peer.preshared_key:
            lines.append(f"PresharedKey = {self.peer.preshared_key}")
        lines.append(f"Endpoint = {self.peer.endpoint}")
        lines.append(f"AllowedIPs = {self.peer.allowed_ips}")
        if self.peer.persistent_keepalive:
            lines.append(f"PersistentKeepalive = {self.peer.persistent_keepalive}")

        return "\n".join(lines) + "\n"


class OpenVPNProfile(BaseModel):
    """OpenVPN client configuration profile and certificate assets."""
    name: str = Field(default="VPN-Private", description="Profile remark / name")
    server_host: str = Field(description="Remote server hostname or IP")
    port: int = Field(default=1194, ge=1, le=65535, description="OpenVPN server port")
    proto: str = Field(default="udp", description="Transport protocol: udp or tcp")
    dev: str = Field(default="tun", description="TUN/TAP device name")
    ca_cert: Optional[str] = Field(default=None, description="PEM Root CA certificate")
    client_cert: Optional[str] = Field(default=None, description="PEM Client certificate")
    client_key: Optional[str] = Field(default=None, description="PEM Client private key")
    tls_auth: Optional[str] = Field(default=None, description="TLS Auth static key")
    tls_crypt: Optional[str] = Field(default=None, description="TLS Crypt key")
    username: Optional[str] = Field(default=None, description="Auth username")
    password: Optional[str] = Field(default=None, description="Auth password")
    cipher: str = Field(default="AES-256-GCM", description="Cipher algorithm")
    auth: str = Field(default="SHA256", description="HMAC authentication digest")
    raw_ovpn: Optional[str] = Field(default=None, description="Raw .ovpn content if pre-generated")
    custom_options: List[str] = Field(default_factory=list, description="Extra OpenVPN directive lines")

    @field_validator("proto")
    @classmethod
    def validate_proto(cls, v: str) -> str:
        v_low = v.lower()
        if v_low not in ("udp", "tcp", "udp4", "tcp4", "udp6", "tcp6"):
            raise ValueError(f"Invalid OpenVPN proto '{v}'. Must be udp, tcp, udp4, tcp4, udp6, or tcp6.")
        return v_low

    @field_validator("dev")
    @classmethod
    def validate_dev(cls, v: str) -> str:
        v_low = v.lower()
        if not (v_low.startswith("tun") or v_low.startswith("tap")):
            raise ValueError(f"Invalid OpenVPN dev '{v}'. Must start with 'tun' or 'tap'.")
        return v_low

    @field_validator("custom_options")
    @classmethod
    def validate_custom_options(cls, v: List[str]) -> List[str]:
        DANGEROUS = {
            "up", "down", "client-connect", "client-disconnect",
            "route-up", "route-pre-down", "auth-user-pass-verify",
            "ipchange", "tls-verify", "plugin", "script-security",
            "management",
        }
        for opt in v:
            first_token = opt.strip().split()[0].lower() if opt.strip() else ""
            if first_token in DANGEROUS:
                raise ValueError(f"Security violation: unsafe directive '{first_token}' not permitted in custom_options.")
        return v

    def to_ovpn(self) -> str:
        if self.raw_ovpn:
            return self.raw_ovpn.strip() + "\n"

        lines = [
            "client",
            f"dev {self.dev}",
            f"proto {self.proto.lower()}",
            f"remote {self.server_host} {self.port}",
            "resolv-retry infinite",
            "nobind",
            "persist-key",
            "persist-tun",
            "remote-cert-tls server",
            f"cipher {self.cipher}",
            f"auth {self.auth}",
        ]
        if self.username:
            lines.append("auth-user-pass")

        for opt in self.custom_options:
            lines.append(opt)

        if self.ca_cert:
            lines.extend(["<ca>", self.ca_cert.strip(), "</ca>"])
        if self.client_cert:
            lines.extend(["<cert>", self.client_cert.strip(), "</cert>"])
        if self.client_key:
            lines.extend(["<key>", self.client_key.strip(), "</key>"])
        if self.tls_auth:
            lines.extend(["key-direction 1", "<tls-auth>", self.tls_auth.strip(), "</tls-auth>"])
        elif self.tls_crypt:
            lines.extend(["<tls-crypt>", self.tls_crypt.strip(), "</tls-crypt>"])

        return "\n".join(lines) + "\n"


class WireGuardClientSpec(BaseModel):
    """WireGuard / AmneziaWG inbound client specification for VPN-UI panels."""
    id: str = Field(description="Client UUID or peer ID")
    email: str = Field(description="Client identification / email")
    public_key: Optional[str] = Field(default=None, description="Client Curve25519 public key")
    private_key: Optional[str] = Field(default=None, description="Client Curve25519 private key")
    preshared_key: Optional[str] = Field(default=None, description="Optional preshared key")
    address: Optional[str] = Field(default=None, description="Allocated tunnel IP (e.g. 10.66.66.2/32)")
    allowed_ips: Optional[str] = Field(default="0.0.0.0/0, ::/0")
    totalGB: int = Field(default=0, description="Quota in bytes")
    expiryTime: int = Field(default=0, description="Expiry in milliseconds")
    enable: bool = Field(default=True)
    tgId: int = Field(default=0)
    subId: str = Field(default="")
    awg_params: Optional[AmneziaWGParams] = Field(default=None)


class OpenVPNClientSpec(BaseModel):
    """OpenVPN client specification for VPN-UI panels."""
    id: str = Field(description="Client UUID or identifier")
    username: str = Field(description="OpenVPN account username")
    password: Optional[str] = Field(default=None, description="OpenVPN account password")
    email: str = Field(description="Client email")
    totalGB: int = Field(default=0, description="Quota in bytes")
    expiryTime: int = Field(default=0, description="Expiry in milliseconds")
    enable: bool = Field(default=True)
    subId: str = Field(default="")
