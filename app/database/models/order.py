"""Order models and state tracking."""
from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base
from app.core.constants import OrderStatus


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False, index=True)
    server_id = Column(UUID(as_uuid=True), ForeignKey("servers.id"), nullable=True, index=True)
    total_price_irr = Column(BigInteger, nullable=False)
    status = Column(String(32), default=OrderStatus.CREATED, nullable=False, index=True)
    idempotency_key = Column(String(128), unique=True, nullable=True, index=True)
    coupon_id = Column(UUID(as_uuid=True), ForeignKey("coupons.id"), nullable=True, index=True)
    provisioning_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", back_populates="orders")
    product = relationship("Product", back_populates="orders")
    subscription = relationship("Subscription", back_populates="order", uselist=False)
    payments = relationship("Payment", back_populates="order")
    coupon = relationship("Coupon")

    @property
    def total_price_toman(self) -> int:
        return self.total_price_irr // 10
