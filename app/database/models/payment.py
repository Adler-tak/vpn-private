"""Payment and Receipt models."""
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
from app.core.constants import PaymentMethod, PaymentStatus


class Payment(Base):
    __tablename__ = "payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=True, index=True) # None for wallet topup
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    amount_irr = Column(BigInteger, nullable=False)
    provider = Column(String(32), default=PaymentMethod.CARD_TO_CARD, nullable=False)
    gateway_id = Column(UUID(as_uuid=True), ForeignKey("payment_gateways.id"), nullable=True, index=True)
    status = Column(String(32), default=PaymentStatus.PENDING, nullable=False, index=True)
    tracking_code = Column(String(128), unique=True, nullable=True, index=True)
    reference_id = Column(String(128), nullable=True) # Gateway RefID / Card-to-Card ref
    admin_id = Column(BigInteger, nullable=True) # Telegram ID of verifying admin
    admin_notes = Column(Text, nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("User", back_populates="payments")
    order = relationship("Order", back_populates="payments")
    receipt = relationship("PaymentReceipt", back_populates="payment", uselist=False, cascade="all, delete-orphan", passive_deletes=True)
    gateway = relationship("PaymentGateway", foreign_keys=[gateway_id])

    @property
    def amount_toman(self) -> int:
        return self.amount_irr // 10


class PaymentReceipt(Base):
    __tablename__ = "payment_receipts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    payment_id = Column(UUID(as_uuid=True), ForeignKey("payments.id", ondelete="CASCADE"), unique=True, nullable=False)
    telegram_file_id = Column(String(255), nullable=True)
    local_file_path = Column(String(255), nullable=True)
    transaction_ref = Column(String(128), nullable=True) # Ref entered by customer
    card_last_4 = Column(String(8), nullable=True)
    submitted_amount_toman = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    payment = relationship("Payment", back_populates="receipt")


class BankCard(Base):
    __tablename__ = "bank_cards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    card_number = Column(String(32), nullable=False)
    card_holder = Column(String(128), nullable=False)
    bank_name = Column(String(64), nullable=False)
    iban = Column(String(64), nullable=True)
    is_active = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class PaymentGateway(Base):
    """Multi-account payment gateway model for SEP, IDPay, ZarinPal, Crypto, etc."""
    __tablename__ = "payment_gateways"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    provider = Column(String(32), nullable=False, index=True)  # "sep", "idpay", "zarinpal", "crypto"
    title = Column(String(128), nullable=False)
    is_active = Column(Boolean, default=False, nullable=False, index=True)
    config = Column(Text, default="{}", nullable=False)
    sort_order = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    @property
    def config_dict(self) -> dict:
        import json
        try:
            return json.loads(self.config or "{}")
        except Exception:
            return {}

    def set_config_dict(self, data: dict):
        import json
        self.config = json.dumps(data, ensure_ascii=False)


async def get_active_gateways(db, provider: str = None) -> list:
    """Fetch all active payment gateways, optionally filtered by provider."""
    from sqlalchemy import select
    if db is None:
        return []
    try:
        query = select(PaymentGateway).where(PaymentGateway.is_active == True)
        if provider:
            query = query.where(PaymentGateway.provider == provider.lower())
        query = query.order_by(PaymentGateway.sort_order.asc(), PaymentGateway.created_at.desc())
        res = await db.execute(query)
        return list(res.scalars().all())
    except Exception:
        return []


async def get_active_gateway_for_provider(db, provider: str):
    """Get an active gateway instance for a provider using load-balancing (random selection)."""
    import random
    gateways = await get_active_gateways(db, provider=provider)
    if gateways:
        return random.choice(gateways)
    return None


async def get_active_bank_card_or_fallback(db) -> dict:
    """Fetch active BankCard from database; if none found, fallback to settings.CARD_NUMBER."""
    from app.core.config import settings
    import random
    from sqlalchemy import select

    if db is not None:
        try:
            cards_res = await db.execute(select(BankCard).where(BankCard.is_active == True))
            active_cards = cards_res.scalars().all()
            if active_cards:
                chosen = random.choice(active_cards)
                return {
                    "id": chosen.id,
                    "card_number": chosen.card_number,
                    "card_holder": chosen.card_holder,
                    "bank_name": chosen.bank_name,
                    "iban": chosen.iban,
                    "is_active": chosen.is_active,
                }
        except Exception:
            pass

    return {
        "id": None,
        "card_number": settings.CARD_NUMBER,
        "card_holder": settings.CARD_HOLDER,
        "bank_name": settings.CARD_BANK_NAME,
        "iban": getattr(settings, "CARD_IBAN", ""),
        "is_active": True,
    }

