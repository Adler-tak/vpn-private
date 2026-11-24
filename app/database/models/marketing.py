"""Marketing and retention campaign models (Win-back, Churn reduction)."""
from datetime import datetime, timezone
import uuid
from sqlalchemy import Column, String, BigInteger, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database.session import Base


class WinbackLog(Base):
    """Tracks sent win-back discount messages to prevent redundant notifications."""
    __tablename__ = "winback_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=True, index=True)
    coupon_code = Column(String(64), nullable=True)
    sent_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User")
