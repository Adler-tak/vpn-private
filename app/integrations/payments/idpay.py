"""IDPay (درگاه آیدی پی) Payment Gateway Integration."""
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


class IDPayGateway(BasePaymentGateway):
    """IDPay REST API v1.1 integration."""

    PAY_URL = "https://api.idpay.ir/v1.1/payment"
    VERIFY_URL = "https://api.idpay.ir/v1.1/payment/verify"

    def __init__(
        self,
        api_key: Optional[str] = None,
        sandbox: Optional[bool] = None,
        callback_url: Optional[str] = None,
    ):
        self.api_key = str(api_key or getattr(settings, "IDPAY_API_KEY", "")).strip()
        self.sandbox = sandbox if sandbox is not None else getattr(settings, "IDPAY_SANDBOX", False)
        self.callback_url = callback_url or f"{settings.SERVER_HOST.rstrip('/')}/api/v1/payments/idpay/callback"

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self.api_key,
        }
        if self.sandbox:
            headers["X-SANDBOX"] = "1"
        return headers

    async def request_payment(
        self,
        payment_id: str,
        amount_irr: int,
        callback_url: Optional[str] = None,
        description: str = "پرداخت سفارش VPN Private",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentInitResult:
        """Create payment request on IDPay."""
        cb_url = callback_url or self.callback_url
        meta = metadata or {}
        tracking_code = meta.get("tracking_code") or f"IDP-{payment_id[:8]}"

        payload = {
            "order_id": tracking_code,
            "amount": amount_irr,
            "name": str(meta.get("name", "کاربر")),
            "phone": str(meta.get("phone", "")),
            "mail": str(meta.get("mail", "")),
            "desc": description,
            "callback": cb_url,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(self.PAY_URL, json=payload, headers=self._get_headers())
                data = resp.json() if resp.text else {}

                if resp.status_code in (200, 201) and "link" in data:
                    authority = str(data.get("id", ""))
                    link = str(data.get("link", ""))
                    return PaymentInitResult(
                        payment_id=payment_id,
                        authority=authority,
                        redirect_url=link,
                        tracking_code=tracking_code,
                        status="PENDING",
                        raw_response=data,
                    )
                else:
                    err_code = data.get("error_code")
                    err_msg = data.get("error_message") or f"خطا در ایجاد تراکنش آیدی‌پی (کد: {err_code})"
                    logger.error(f"IDPay request error: {err_msg}")
                    return PaymentInitResult(
                        payment_id=payment_id,
                        status="FAILED",
                        instructions=f"خطا در درگاه آیدی پی: {err_msg}",
                        raw_response=data,
                    )
            except Exception as e:
                logger.error(f"IDPay request failed exception: {e}")
                return PaymentInitResult(
                    payment_id=payment_id,
                    status="FAILED",
                    instructions=f"عدم امکان برقراری ارتباط با درگاه آیدی پی: {e}",
                )

    async def verify_payment(
        self,
        authority: str,
        amount_irr: int,
        order_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentVerifyResult:
        """Verify payment with IDPay server-to-server."""
        payload = {
            "id": authority,
            "order_id": order_id or authority,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.post(self.VERIFY_URL, json=payload, headers=self._get_headers())
                data = resp.json() if resp.text else {}

                status = data.get("status")
                # 100 = Payment confirmed, 101 = Payment already confirmed
                if status in (100, 101, "100", "101"):
                    track_id = str(data.get("track_id", ""))
                    payment_info = data.get("payment") or {}
                    card_pan = payment_info.get("card_no") or ""
                    return PaymentVerifyResult(
                        success=True,
                        amount_irr=amount_irr,
                        reference_id=track_id,
                        card_pan=card_pan,
                        raw_response=data,
                    )
                else:
                    err_msg = data.get("error_message") or f"تایید تراکنش ناموفق بود (کد وضعیت: {status})"
                    return PaymentVerifyResult(
                        success=False,
                        error_message=err_msg,
                        raw_response=data,
                    )
            except Exception as e:
                logger.error(f"IDPay verify failed exception: {e}")
                return PaymentVerifyResult(
                    success=False,
                    error_message=f"خطا در اتصال به سرور تایید تراکنش آیدی پی: {e}",
                )

    def validate_callback(self, params: Dict[str, Any]) -> bool:
        """Check if IDPay callback status indicates user completed payment."""
        status = str(params.get("status", ""))
        return status in ("10", "100", "101")
