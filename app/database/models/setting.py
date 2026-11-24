"""System settings database model for dynamic configuration (3-to-97 rule)."""
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime
from app.database.session import Base


class SystemSetting(Base):
    """
    Clean, organized system settings table.
    Stores dynamic application configuration categorized by domain.
    """
    __tablename__ = "system_settings"

    key = Column(String(64), primary_key=True, index=True, nullable=False)
    value = Column(Text, nullable=True)
    category = Column(String(32), index=True, default="general", nullable=False)
    description = Column(String(255), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<SystemSetting(key='{self.key}', category='{self.category}', value='{self.value}')>"
