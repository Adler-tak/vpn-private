"""Double-entry accounting and ledger models."""
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
from app.database.session import Base
from app.core.constants import AccountType


class AccountingAccount(Base):
    __tablename__ = "accounting_accounts"

    code = Column(String(32), primary_key=True) # e.g. "1010", "2010", "4010"
    name = Column(String(128), nullable=False)
    account_type = Column(String(32), nullable=False) # ASSET, LIABILITY, EQUITY, REVENUE, EXPENSE
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class AccountingEntry(Base):
    __tablename__ = "accounting_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    transaction_group_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    debit_account_code = Column(String(32), ForeignKey("accounting_accounts.code"), nullable=False, index=True)
    credit_account_code = Column(String(32), ForeignKey("accounting_accounts.code"), nullable=False, index=True)
    amount_irr = Column(BigInteger, nullable=False) # Always positive
    reference_type = Column(String(64), nullable=False) # ORDER, TOPUP, REFUND, COMMISSION, ADJUSTMENT
    reference_id = Column(String(128), nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    @property
    def amount_toman(self) -> int:
        return self.amount_irr // 10
