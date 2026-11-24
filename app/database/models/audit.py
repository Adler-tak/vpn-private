"""Audit logs and Idempotency tracking models."""
from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from app.database.session import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    actor_id = Column(BigInteger, nullable=True, index=True) # Telegram ID or 0 for system
    actor_role = Column(String(32), default="SYSTEM", nullable=False)
    action = Column(String(128), nullable=False, index=True) # PAYMENT_APPROVED, PANEL_ADDED, etc.
    target_type = Column(String(64), nullable=True) # User, Payment, Panel, Subscription
    target_id = Column(String(128), nullable=True)
    details = Column(Text, nullable=True) # JSON or descriptive summary
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    key = Column(String(128), primary_key=True, index=True)
    handler_name = Column(String(64), nullable=False)
    status = Column(String(32), default="PROCESSING", nullable=False) # PROCESSING, COMPLETED, FAILED
    response_payload = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
