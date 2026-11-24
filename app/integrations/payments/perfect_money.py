"""Perfect Money payment gateway implementation (SCI / API)."""
import hashlib
import logging
import urllib.parse
from typing import Any, Dict, Optional

from app.core.config import settings
from app.integrations.payments.base import BasePaymentGateway, PaymentInitResult, PaymentVerifyResult

logger = logging.getLogger(__name__)


class PerfectMoneyGateway(BasePaymentGateway):
    """
    Integration with Perfect Money Standard Client Interface (SCI).
    Supports USD payments converted dynamically from Iranian Toman.
    """

    SCI_URL = "https://perfectmoney.com/api/step1.asp"

    def __init__(
        self,
        account_id: Optional[str] = None,
        passphrase: Optional[str] = None,
        usd_rate: Optional[int] = None,
    ):
        self.account_id = (account_id or getattr(settings, "PERFECT_MONEY_ACCOUNT", "U12345678")).strip()
        self.passphrase = (passphrase or getattr(settings, "PERFECT_MONEY_PASSPHRASE", "")).strip()
        self.usd_rate = usd_rate or getattr(settings, "PERFECT_MONEY_USD_RATE", 70000)

    def calculate_usd_amount(self, amount_irr: int) -> float:
        """Convert IRR to USD based on current rate (IRR -> Toman / Rate)."""
        amount_toman = amount_irr // 10
        usd = round(amount_toman / self.usd_rate, 2)
        return max(1.0, usd)

    async def request_payment(
        self,
        payment_id: str,
        amount_irr: int,
        callback_url: str = "",
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentInitResult:
        """Generate Perfect Money payment request and redirect URL / parameters."""
        usd_amount = self.calculate_usd_amount(amount_irr)
        tracking_code = f"PM-{payment_id[:8].upper()}"

        cb_url = callback_url or getattr(settings, "PERFECT_MONEY_CALLBACK_URL", "http://localhost:8000/api/v1/payments/perfectmoney/callback")
        status_url = getattr(settings, "PERFECT_MONEY_STATUS_URL", "http://localhost:8000/api/v1/payments/perfectmoney/status")

        params = {
            "PAYEE_ACCOUNT": self.account_id,
            "PAYEE_NAME": "VPN Private",
            "PAYMENT_AMOUNT": f"{usd_amount:.2f}",
            "PAYMENT_UNITS": "USD",
            "PAYMENT_ID": tracking_code,
            "STATUS_URL": status_url,
            "PAYMENT_URL": cb_url,
            "NOPAYMENT_URL": cb_url,
            "BAGGAGE_FIELDS": "PAYMENT_ID",
            "ORDER_NUM": payment_id,
        }

        query_str = urllib.parse.urlencode(params)
        redirect_url = f"{self.SCI_URL}?{query_str}"

        instructions = (
            f"💵 <b>پرداخت با پرفکت مانی (Perfect Money):</b>\n\n"
            f"💰 مبلغ دلاری: <b>{usd_amount:.2f} USD</b>\n"
            f"📍 شماره حساب پرفکت مانی: <code>{self.account_id}</code>\n"
            f"🔖 شناسه سفارش: <code>{tracking_code}</code>\n"
            f"💵 معادل تومانی: <b>{amount_irr // 10:,} تومان</b> (نرخ هر دلار: {self.usd_rate:,} تومان)\n\n"
            f"جهت پرداخت مستقیم، روی دکمه درگاه پرفکت مانی کلیک نمایید یا مبلغ فوق را به حساب بالا واریز و کد پیگیری (Batch) را ارسال فرمایید."
        )

        return PaymentInitResult(
            payment_id=payment_id,
            authority=tracking_code,
            tracking_code=tracking_code,
            redirect_url=redirect_url,
            instructions=instructions,
            status="PENDING",
            raw_response=params,
        )

    def validate_callback(self, params: Dict[str, Any]) -> bool:
        """
        Validate incoming IPN V2_HASH from Perfect Money.
        MD5(PAYMENT_ID:PAYEE_ACCOUNT:PAYMENT_AMOUNT:PAYMENT_UNITS:PAYMENT_BATCH_NUM:PAYER_ACCOUNT:AlternatePassphraseHash:TIMESTAMPGMT)
        """
        payment_id = str(params.get("PAYMENT_ID") or "")
        payee_account = str(params.get("PAYEE_ACCOUNT") or "")
        payment_amount = str(params.get("PAYMENT_AMOUNT") or "")
        payment_units = str(params.get("PAYMENT_UNITS") or "")
        payment_batch_num = str(params.get("PAYMENT_BATCH_NUM") or "")
        payer_account = str(params.get("PAYER_ACCOUNT") or "")
        timestampgmt = str(params.get("TIMESTAMPGMT") or "")
        v2_hash = str(params.get("V2_HASH") or "").strip().upper()

        # If no passphrase configured in test/sandbox, verify essential fields
        if not self.passphrase:
            return bool(payee_account == self.account_id and payment_batch_num)

        passphrase_hash = hashlib.md5(self.passphrase.encode("utf-8")).hexdigest().upper()
        raw = f"{payment_id}:{payee_account}:{payment_amount}:{payment_units}:{payment_batch_num}:{payer_account}:{passphrase_hash}:{timestampgmt}"
        expected_hash = hashlib.md5(raw.encode("utf-8")).hexdigest().upper()

        if expected_hash == v2_hash:
            return True

        logger.warning(f"Perfect Money V2_HASH mismatch. Expected: {expected_hash}, got: {v2_hash}")
        return False

    async def verify_payment(
        self,
        authority: str,
        amount_irr: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentVerifyResult:
        """Verify payment parameters returned by callback/webhook."""
        meta = metadata or {}
        batch_num = str(meta.get("PAYMENT_BATCH_NUM") or meta.get("batch") or "")

        if not self.validate_callback(meta):
            # If batch num present and sandbox
            if batch_num and getattr(settings, "ENVIRONMENT", "").lower() in ("test", "development"):
                return PaymentVerifyResult(
                    success=True,
                    amount_irr=amount_irr,
                    reference_id=batch_num,
                    raw_response=meta,
                )
            return PaymentVerifyResult(
                success=False,
                error_message="اعتبارسنجی امضای پرفکت مانی (V2_HASH) ناموفق بود.",
                raw_response=meta,
            )

        return PaymentVerifyResult(
            success=True,
            amount_irr=amount_irr,
            reference_id=batch_num or f"PM-BAT-{authority}",
            raw_response=meta,
        )
