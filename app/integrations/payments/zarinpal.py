"""Zarinpal Payment Gateway v4 implementation."""
import logging
from typing import Any, Dict, Optional
import httpx
from app.integrations.payments.base import (
    BasePaymentGateway,
    PaymentInitResult,
    PaymentVerifyResult,
)
from app.core.config import settings

logger = logging.getLogger(__name__)


class ZarinpalGateway(BasePaymentGateway):
    """Zarinpal REST API v4 integration with Sandbox & Production modes."""

    def __init__(
        self,
        merchant_id: Optional[str] = None,
        sandbox: Optional[bool] = None,
        callback_url: Optional[str] = None,
    ):
        self.merchant_id = merchant_id or settings.ZARINPAL_MERCHANT_ID
        self.sandbox = sandbox if sandbox is not None else settings.ZARINPAL_SANDBOX
        self.callback_url = callback_url or settings.ZARINPAL_CALLBACK_URL

        if self.sandbox:
            self.base_url = "https://sandbox.zarinpal.com/pg/v4/payment"
            self.start_pay_url = "https://sandbox.zarinpal.com/pg/StartPay"
        else:
            self.base_url = "https://api.zarinpal.com/pg/v4/payment"
            self.start_pay_url = "https://www.zarinpal.com/pg/StartPay"

    async def request_payment(
        self,
        payment_id: str,
        amount_irr: int,
        callback_url: Optional[str] = None,
        description: str = "پرداخت سفارش VPN Private",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentInitResult:
        url = f"{self.base_url}/request.json"
        cb_url = callback_url or self.callback_url
        amount_toman = amount_irr // 10

        payload = {
            "merchant_id": self.merchant_id,
            "amount": amount_toman,
            "currency": "IRT",
            "description": description,
            "callback_url": cb_url,
            "metadata": metadata or {},
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(url, json=payload)
                data = resp.json().get("data", {})
                errors = resp.json().get("errors", {})

                if resp.status_code == 200 and data.get("code") == 100:
                    authority = data["authority"]
                    redirect = f"{self.start_pay_url}/{authority}"
                    return PaymentInitResult(
                        payment_id=payment_id,
                        authority=authority,
                        redirect_url=redirect,
                        tracking_code=authority,
                        status="PENDING",
                        raw_response=resp.json(),
                    )
                else:
                    msg = errors.get("message", "Error requesting Zarinpal payment")
                    logger.error(f"Zarinpal request error: {msg}")
                    return PaymentInitResult(
                        payment_id=payment_id,
                        status="FAILED",
                        instructions=f"خطا در اتصال به درگاه زرین پال: {msg}",
                        raw_response=resp.json(),
                    )
            except Exception as e:
                logger.error(f"Zarinpal request failed: {e}")
                return PaymentInitResult(
                    payment_id=payment_id,
                    status="FAILED",
                    instructions=f"عدم امکان برقراری ارتباط با درگاه زرین پال: {e}",
                )

    async def verify_payment(
        self,
        authority: str,
        amount_irr: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentVerifyResult:
        url = f"{self.base_url}/verify.json"
        amount_toman = amount_irr // 10

        payload = {
            "merchant_id": self.merchant_id,
            "amount": amount_toman,
            "authority": authority,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(url, json=payload)
                result = resp.json()
                data = result.get("data", {})
                code = data.get("code")

                # 100 = First verification success, 101 = Already verified (idempotent)
                if code in (100, 101):
                    ref_id = str(data.get("ref_id", ""))
                    card_pan = data.get("card_pan", "")
                    fee = data.get("fee", 0) * 10 # Fee in IRR
                    return PaymentVerifyResult(
                        success=True,
                        amount_irr=amount_irr,
                        reference_id=ref_id,
                        card_pan=card_pan,
                        fee_irr=fee,
                        raw_response=result,
                    )
                else:
                    msg = result.get("errors", {}).get("message", f"Verification failed with code {code}")
                    return PaymentVerifyResult(
                        success=False,
                        error_message=msg,
                        raw_response=result,
                    )
            except Exception as e:
                logger.error(f"Zarinpal verify failed: {e}")
                return PaymentVerifyResult(
                    success=False,
                    error_message=str(e),
                )

    def validate_callback(self, params: Dict[str, Any]) -> bool:
        """Validate if callback indicates success (Status == OK)."""
        status = params.get("Status", "")
        return status.upper() == "OK"
