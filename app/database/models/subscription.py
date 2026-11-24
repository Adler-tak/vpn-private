"""Subscription models and lifecycle."""
from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base
from app.core.constants import SubscriptionStatus


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), unique=True, nullable=True)
    server_id = Column(UUID(as_uuid=True), ForeignKey("servers.id"), nullable=False, index=True)
    panel_id = Column(UUID(as_uuid=True), ForeignKey("panels.id"), nullable=False, index=True)
    inbound_id = Column(UUID(as_uuid=True), ForeignKey("inbounds.id"), nullable=False, index=True)
    client_uuid = Column(String(64), nullable=False, index=True)
    client_email = Column(String(128), unique=True, nullable=False, index=True)
    protocol = Column(String(32), default="vless", nullable=False)
    subscription_token = Column(String(64), unique=True, nullable=False, index=True) # subId in 3x-ui
    traffic_limit_bytes = Column(BigInteger, default=0, nullable=False) # 0 = unlimited
    traffic_used_bytes = Column(BigInteger, default=0, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    status = Column(String(32), default=SubscriptionStatus.ACTIVE, nullable=False, index=True)
    last_grace_at = Column(DateTime(timezone=True), nullable=True)
    auto_renew = Column(Boolean, default=False, nullable=False)
    alert_traffic_sent = Column(Boolean, default=False, nullable=False)
    alert_time_sent = Column(Boolean, default=False, nullable=False)
    last_auto_renew_attempt = Column(DateTime(timezone=True), nullable=True)
    allowed_locations = Column(Text, nullable=True)  # Comma-separated location codes or ALL for manual configs
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", back_populates="subscriptions")
    order = relationship("Order", back_populates="subscription")
    server = relationship("Server", back_populates="subscriptions")
    inbound = relationship("Inbound", back_populates="subscriptions")

    @property
    def remaining_bytes(self) -> int:
        if self.traffic_limit_bytes <= 0:
            return 999_999_999_999 # unlimited
        return max(0, self.traffic_limit_bytes - self.traffic_used_bytes)

    @property
    def remaining_gb(self) -> float:
        return round(self.remaining_bytes / (1024 ** 3), 2)

    @property
    def traffic_used_gb(self) -> float:
        return round(self.traffic_used_bytes / (1024 ** 3), 2)

    @property
    def traffic_limit_gb(self) -> float:
        return round(self.traffic_limit_bytes / (1024 ** 3), 2)
