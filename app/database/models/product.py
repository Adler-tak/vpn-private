"""Product and pricing catalog models."""
from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    traffic_bytes = Column(BigInteger, nullable=False) # Quota in bytes, 0 = unlimited
    duration_days = Column(Integer, nullable=False) # Period in days
    price_irr = Column(BigInteger, nullable=False) # Base canonical price in IRR
    protocol = Column(String(32), default="vless", nullable=False) # Preferred protocol
    allowed_locations = Column(Text, default="ALL", nullable=False) # comma-separated codes/inbounds or ALL
    is_active = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    renewal_discount_percent = Column(Integer, default=5, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    orders = relationship("Order", back_populates="product")

    @property
    def price_toman(self) -> int:
        return self.price_irr // 10

    @property
    def traffic_gb(self) -> float:
        if self.traffic_bytes == 0:
            return 0.0
        return round(self.traffic_bytes / (1024 ** 3), 1)

    @property
    def traffic_display(self) -> str:
        if self.traffic_bytes == 0:
            return "نامحدود"
        return f"{self.traffic_gb} گیگابایت"
