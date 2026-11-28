"""Panel, Inbound, and Server infrastructure models."""
from datetime import datetime, timezone
from typing import Optional
import uuid
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship, reconstructor, validates
from app.database.session import Base
from app.core.security import decrypt_credential, encrypt_credential


class Panel(Base):
    __tablename__ = "panels"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(128), unique=True, nullable=False)
    base_url = Column(String(255), nullable=False)
    panel_type = Column(String(32), default="3x-ui", server_default="3x-ui", nullable=False)
    username = Column(String(128), nullable=False)
    _password_encrypted = Column("password_encrypted", Text, nullable=False)
    _api_token_encrypted = Column("api_token_encrypted", Text, nullable=True)
    _login_secret_encrypted = Column("login_secret_encrypted", Text, nullable=True)
    version = Column(String(32), default="v3.11", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_healthy = Column(Boolean, default=True, nullable=False)
    error_message = Column(Text, nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    inbounds = relationship("Inbound", back_populates="panel", cascade="all, delete-orphan", passive_deletes=True)
    servers = relationship("Server", back_populates="panel")

    def __init__(self, **kwargs):
        if not kwargs.get("panel_type") or str(kwargs.get("panel_type")).strip().lower() in ("none", "null", ""):
            kwargs["panel_type"] = "3x-ui"
        raw_uri = kwargs.pop("uri_path", None) or kwargs.pop("web_base_path", None)
        if raw_uri and "base_url" in kwargs:
            from app.integrations.xui.client import normalize_panel_base_url
            kwargs["base_url"] = normalize_panel_base_url(kwargs["base_url"], uri_path=raw_uri)
        if kwargs.get("api_token") or kwargs.get("_api_token_encrypted"):
            if not kwargs.get("username"):
                kwargs["username"] = "token_user"
            if not kwargs.get("password") and not kwargs.get("_password_encrypted"):
                kwargs["_password_encrypted"] = encrypt_credential("API_TOKEN_AUTH")
        super().__init__(**kwargs)
        if raw_uri:
            clean = "/" + str(raw_uri).strip().strip("/") + "/"
            self._uri_path_override = clean

    @validates("panel_type")
    def validate_panel_type(self, key, value):
        if not value or str(value).strip().lower() in ("none", "null", ""):
            return "3x-ui"
        return str(value).strip()

    @reconstructor
    def init_on_load(self):
        if not getattr(self, "panel_type", None) or str(self.panel_type).strip().lower() in ("none", "null", ""):
            self.panel_type = "3x-ui"

    @property
    def uri_path(self) -> Optional[str]:
        if getattr(self, "_uri_path_override", None):
            return self._uri_path_override
        if not self.base_url:
            return None
        import urllib.parse
        parsed = urllib.parse.urlparse(self.base_url)
        path = parsed.path.strip()
        if path and path != "/":
            return "/" + path.strip("/") + "/"
        return None

    @uri_path.setter
    def uri_path(self, val: Optional[str]):
        if val and str(val).strip():
            clean = "/" + str(val).strip().strip("/") + "/"
            self._uri_path_override = clean
            if self.base_url:
                from app.integrations.xui.client import normalize_panel_base_url
                self.base_url = normalize_panel_base_url(self.base_url, uri_path=clean)
        else:
            self._uri_path_override = None

    @property
    def web_base_path(self) -> Optional[str]:
        return self.uri_path

    @web_base_path.setter
    def web_base_path(self, val: Optional[str]):
        self.uri_path = val

    @property
    def password(self) -> str:
        if not self._password_encrypted:
            return ""
        try:
            return decrypt_credential(self._password_encrypted)
        except Exception:
            return ""

    @password.setter
    def password(self, raw_pwd: Optional[str]):
        if not raw_pwd:
            if getattr(self, "api_token", None) or getattr(self, "_api_token_encrypted", None):
                self._password_encrypted = encrypt_credential("API_TOKEN_AUTH")
            else:
                self._password_encrypted = ""
        else:
            self._password_encrypted = encrypt_credential(raw_pwd)

    @property
    def api_token(self) -> str:
        if not self._api_token_encrypted:
            return ""
        return decrypt_credential(self._api_token_encrypted)

    @api_token.setter
    def api_token(self, raw_token: Optional[str]):
        self._api_token_encrypted = encrypt_credential(raw_token) if raw_token else None
        if raw_token:
            if not getattr(self, "username", None):
                self.username = "token_user"
            if not getattr(self, "_password_encrypted", None):
                self._password_encrypted = encrypt_credential("API_TOKEN_AUTH")

    @property
    def login_secret(self) -> str:
        if not self._login_secret_encrypted:
            return ""
        return decrypt_credential(self._login_secret_encrypted)

    @login_secret.setter
    def login_secret(self, raw_secret: str):
        self._login_secret_encrypted = encrypt_credential(raw_secret) if raw_secret else None

    @property
    def otp_secret(self) -> str:
        return self.login_secret

    @otp_secret.setter
    def otp_secret(self, raw_secret: str):
        self.login_secret = raw_secret


class Inbound(Base):
    __tablename__ = "inbounds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    panel_id = Column(UUID(as_uuid=True), ForeignKey("panels.id", ondelete="CASCADE"), nullable=False, index=True)
    remote_inbound_id = Column(Integer, nullable=False) # ID inside 3x-ui SQLite
    protocol = Column(String(32), nullable=False) # vless, vmess, trojan, shadowsocks
    port = Column(Integer, nullable=False)
    listen = Column(String(64), default="0.0.0.0", nullable=False)
    tag = Column(String(128), nullable=True)
    remark = Column(String(255), nullable=True)
    stream_settings = Column(Text, nullable=True) # JSON streamSettings
    is_active = Column(Boolean, default=True, nullable=False)
    client_count = Column(Integer, default=0, nullable=False)
    max_clients = Column(Integer, default=500, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    panel = relationship("Panel", back_populates="inbounds")
    servers = relationship("Server", back_populates="inbound")
    subscriptions = relationship("Subscription", back_populates="inbound")


class Server(Base):
    __tablename__ = "servers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    inbound_id = Column(UUID(as_uuid=True), ForeignKey("inbounds.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String(128), unique=True, nullable=False)
    location_code = Column(String(16), nullable=False, index=True) # DE, FI, TR, NL, etc.
    location_name = Column(String(64), nullable=False) # آلمان, فنلاند, ترکیه
    flag_emoji = Column(String(16), default="🌐", nullable=False)
    priority = Column(Integer, default=10, nullable=False) # Higher means preferred
    panel_id = Column(UUID(as_uuid=True), ForeignKey("panels.id"), nullable=False, index=True)
    subscription_host = Column(String(255), nullable=False) # Domain or IP for sub links
    subscription_port = Column(Integer, default=2096, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    capacity_limit = Column(Integer, default=1000, nullable=False)
    current_clients = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    panel = relationship("Panel", back_populates="servers")
    inbound = relationship("Inbound", back_populates="servers")
    subscriptions = relationship("Subscription", back_populates="server")


class BalancerLocation(Base):
    __tablename__ = "balancer_locations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    country_code = Column(String(16), unique=True, nullable=False, index=True) # de, us, tr, ca, etc.
    balancer_tag = Column(String(32), unique=True, nullable=False, index=True) # DE, US-BL, TR, CA, etc.
    name_en = Column(String(64), nullable=False)
    name_fa = Column(String(64), nullable=False)
    flag_emoji = Column(String(16), default="🌐", nullable=False)
    is_active = Column(Boolean, default=False, nullable=False, index=True) # Activated by admin for balancer
    is_supported_pia = Column(Boolean, default=False, nullable=False)
    is_supported_wd = Column(Boolean, default=False, nullable=False)
    active_provider = Column(String(32), default="both", nullable=False) # 'both', 'pia', 'windscribe'
    sort_order = Column(Integer, default=100, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

