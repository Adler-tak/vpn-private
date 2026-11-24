"""Card-to-Card payment provider implementation."""
import secrets
from typing import Any, Dict, Optional
from app.integrations.payments.base import (
    BasePaymentGateway,
    PaymentInitResult,
    PaymentVerifyResult,
)
from app.core.config import settings


class CardToCardGateway(BasePaymentGateway):
    """Card to Card manual transfer flow."""

    def __init__(
        self,
        card_number: Optional[str] = None,
        card_holder: Optional[str] = None,
        bank_name: Optional[str] = None,
        iban: Optional[str] = None,
    ):
        self._explicit_card_number = card_number
        self._explicit_card_holder = card_holder
        self._explicit_bank_name = bank_name
        self._explicit_iban = iban

    @property
    def card_number(self) -> str:
        return self._explicit_card_number or settings.CARD_NUMBER

    @property
    def card_holder(self) -> str:
        return self._explicit_card_holder or settings.CARD_HOLDER

    @property
    def bank_name(self) -> str:
        return self._explicit_bank_name or settings.CARD_BANK_NAME

    @property
    def iban(self) -> str:
        return self._explicit_iban or settings.CARD_IBAN

    async def request_payment(
        self,
        payment_id: str,
        amount_irr: int,
        callback_url: str,
        description: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentInitResult:
        tracking_code = f"C2C-{secrets.token_hex(4).upper()}"
        amount_toman = amount_irr // 10

        instructions = (
            f"💳 **اطلاعات واریز کارت به کارت:**\n\n"
            f"🏦 بانک: {self.bank_name}\n"
            f"🔢 شماره کارت: `{self.card_number}`\n"
            f"👤 به نام: {self.card_holder}\n"
            f"💰 مبلغ دقیق: **{amount_toman:,} تومان** ({amount_irr:,} ریال)\n"
            f"🔖 شناسه پیگیری سیستم: `{tracking_code}`\n\n"
            f"⚠️ لطفاً پس از واریز، تصویر فیش یا ۴ رقم آخر کارت و کد پیگیری را در بات ارسال نمایید."
        )

        return PaymentInitResult(
            payment_id=payment_id,
            authority=tracking_code,
            tracking_code=tracking_code,
            instructions=instructions,
            status="PENDING",
            raw_response={"card_number": self.card_number, "bank": self.bank_name},
        )

    async def verify_payment(
        self,
        authority: str,
        amount_irr: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentVerifyResult:
        # Manual verification is controlled by Admin via Telegram Panel
        return PaymentVerifyResult(
            success=True,
            amount_irr=amount_irr,
            reference_id=authority,
            card_pan=metadata.get("card_last_4", "") if metadata else "",
        )

    def validate_callback(self, params: Dict[str, Any]) -> bool:
        return True
