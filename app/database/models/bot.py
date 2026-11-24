"""Telegram Bot management database model."""
from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from app.database.session import Base


class TelegramBot(Base):
    __tablename__ = "telegram_bots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=True)
    token = Column(String(255), unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True, index=True)
    is_active = Column(Boolean, default=False, nullable=False, index=True)
    proxy_url = Column(String(512), nullable=True)
    proxy_enabled = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
