"""Reseller, Referral, and Coupon models."""
from datetime import datetime, timezone
import uuid
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


class Reseller(Base):
    __tablename__ = "resellers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False)
    discount_percent = Column(Integer, default=20, nullable=False)
    credit_limit_irr = Column(BigInteger, default=0, nullable=False) # For post-paid or extended credit
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User")


class Referral(Base):
    __tablename__ = "referrals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    referrer_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    referred_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False)
    status = Column(String(32), default="PENDING", nullable=False) # PENDING, QUALIFIED, REWARDED
    total_commission_irr = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    referrer = relationship("User", foreign_keys=[referrer_user_id])
    referred = relationship("User", foreign_keys=[referred_user_id])


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    discount_percent = Column(Integer, default=0, nullable=False) # e.g. 15 for 15%
    discount_amount_irr = Column(BigInteger, default=0, nullable=False) # Fixed discount amount
    max_uses = Column(Integer, default=100, nullable=False)
    current_uses = Column(Integer, default=0, nullable=False)
    valid_until = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    @property
    def discount_amount_toman(self) -> int:
        return self.discount_amount_irr // 10 if self.discount_amount_irr else 0

    @discount_amount_toman.setter
    def discount_amount_toman(self, val: int):
        self.discount_amount_irr = (val or 0) * 10


class LuckyWheelSpin(Base):
    __tablename__ = "lucky_wheel_spins"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=True, index=True)
    prize_label = Column(String(128), nullable=False)
    prize_type = Column(String(32), nullable=False)  # wallet_irr, traffic_mb, coupon_percent, empty
    prize_amount = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    user = relationship("User")


