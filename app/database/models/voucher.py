"""Voucher scratch card model."""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class Voucher(Base):
    __tablename__ = "vouchers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    code = Column(String(64), unique=True, index=True, nullable=False)
    username = Column(String(128), index=True, nullable=True)
    password = Column(String(128), nullable=True)
    duration_months = Column(Integer, default=1, nullable=True)
    duration_days = Column(Integer, default=30, nullable=True)
    voucher_type = Column(String(32), default="plan", nullable=False)  # "plan" or "wallet_credit"
    amount_toman = Column(BigInteger, default=0, nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=True, index=True)
    is_used = Column(Boolean, default=False, index=True, nullable=False)
    used_by_telegram_id = Column(BigInteger, nullable=True, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    batch_id = Column(String(64), index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    product = relationship("Product", foreign_keys=[product_id], backref="vouchers")
