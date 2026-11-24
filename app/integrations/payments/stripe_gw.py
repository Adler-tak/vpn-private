"""Stripe International Multi-Currency Payment Gateway Integration."""
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


class StripeGateway(BasePaymentGateway):
    """Stripe Checkout Sessions REST API v1 integration (Visa / Mastercard / Apple Pay / Google Pay)."""

    SESSION_URL = "https://api.stripe.com/v1/checkout/sessions"

    def __init__(
        self,
        secret_key: Optional[str] = None,
        publishable_key: Optional[str] = None,
        currency: str = "USD",
        exchange_rate_toman: int = 70000,
        callback_url: Optional[str] = None,
    ):
        self.secret_key = str(secret_key or getattr(settings, "STRIPE_SECRET_KEY", "")).strip()
        self.publishable_key = str(publishable_key or getattr(settings, "STRIPE_PUBLISHABLE_KEY", "")).strip()
        self.currency = str(currency or getattr(settings, "STRIPE_CURRENCY", "USD")).strip().lower()
        try:
            self.exchange_rate_toman = max(1, int(exchange_rate_toman or getattr(settings, "USDT_TOMAN_RATE", 70000)))
        except Exception:
            self.exchange_rate_toman = 70000
        self.callback_url = callback_url or f"{settings.SERVER_HOST.rstrip('/')}/api/v1/payments/stripe/callback"

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def request_payment(
        self,
        payment_id: str,
        amount_irr: int,
        callback_url: Optional[str] = None,
        description: str = "VPN Private Subscription",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentInitResult:
        """Create a Stripe Checkout Session."""
        cb_url = callback_url or self.callback_url
        meta = metadata or {}
        tracking_code = meta.get("tracking_code") or f"STRP-{payment_id[:8].upper()}"

        if not self.secret_key:
            return PaymentInitResult(
                payment_id=payment_id,
                status="FAILED",
                instructions="کلید Secret Key درگاه استرایپ در پنل مدیریت تنظیم نشده است.",
            )

        amount_toman = max(1000, amount_irr // 10)
        # Convert Toman to target currency smallest unit (cents, minimum 50 cents for Stripe)
        unit_amount_float = amount_toman / float(self.exchange_rate_toman)
        unit_amount_cents = max(50, int(round(unit_amount_float * 100)))

        sep_char = "&" if "?" in cb_url else "?"
        success_url = f"{cb_url}{sep_char}session_id={{CHECKOUT_SESSION_ID}}&tracking_code={tracking_code}"
        cancel_url = f"{cb_url}{sep_char}canceled=1&tracking_code={tracking_code}"

        form_data = {
            "mode": "payment",
            "client_reference_id": tracking_code,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][price_data][currency]": self.currency,
            "line_items[0][price_data][unit_amount]": str(unit_amount_cents),
            "line_items[0][price_data][product_data][name]": description,
            "line_items[0][quantity]": "1",
            "metadata[payment_id]": str(payment_id),
            "metadata[tracking_code]": str(tracking_code),
            "metadata[amount_irr]": str(amount_irr),
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.post(self.SESSION_URL, data=form_data, headers=self._get_headers())
                data = resp.json() if resp.text else {}

                if resp.status_code in (200, 201) and data.get("url"):
                    session_id = str(data.get("id", ""))
                    checkout_url = str(data.get("url", ""))
                    return PaymentInitResult(
                        payment_id=payment_id,
                        authority=session_id,
                        redirect_url=checkout_url,
                        tracking_code=tracking_code,
                        status="PENDING",
                        raw_response=data,
                    )
                else:
                    err_obj = data.get("error") or {}
                    err_msg = err_obj.get("message") or f"خطا در اتصال به درگاه استرایپ (HTTP {resp.status_code})"
                    logger.error(f"Stripe session creation error: {err_msg}")
                    return PaymentInitResult(
                        payment_id=payment_id,
                        status="FAILED",
                        instructions=f"خطا در درگاه استرایپ: {err_msg}",
                        raw_response=data,
                    )
            except Exception as e:
                logger.error(f"Stripe request failed exception: {e}")
                return PaymentInitResult(
                    payment_id=payment_id,
                    status="FAILED",
                    instructions=f"عدم امکان برقراری ارتباط با سرور استرایپ: {e}",
                )

    async def verify_payment(
        self,
        authority: str,
        amount_irr: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentVerifyResult:
        """Verify Stripe Checkout Session status server-to-server."""
        if not authority or not self.secret_key:
            return PaymentVerifyResult(
                success=False,
                error_message="شناسه نشست استرایپ یا کلید API نامعتبر است.",
            )

        url = f"{self.SESSION_URL}/{authority}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.get(url, headers={"Authorization": f"Bearer {self.secret_key}"})
                data = resp.json() if resp.text else {}

                payment_status = str(data.get("payment_status", "")).lower()
                session_status = str(data.get("status", "")).lower()

                if resp.status_code == 200 and payment_status == "paid":
                    payment_intent = str(data.get("payment_intent") or authority)
                    return PaymentVerifyResult(
                        success=True,
                        amount_irr=amount_irr,
                        reference_id=payment_intent,
                        raw_response=data,
                    )
                else:
                    err_obj = data.get("error") or {}
                    err_msg = err_obj.get("message") or f"وضعیت پرداخت در استرایپ تکمیل نشده است ({payment_status or session_status})"
                    return PaymentVerifyResult(
                        success=False,
                        error_message=err_msg,
                        raw_response=data,
                    )
            except Exception as e:
                logger.error(f"Stripe verify exception: {e}")
                return PaymentVerifyResult(
                    success=False,
                    error_message=f"خطا در استعلام تراکنش از استرایپ: {e}",
                )

    def validate_callback(self, params: Dict[str, Any]) -> bool:
        """Validate incoming callback parameters (session_id present and not canceled)."""
        if not params:
            return False
        if params.get("canceled") in (True, "1", "true", 1):
            return False
        session_id = params.get("session_id") or params.get("authority")
        return bool(session_id and str(session_id).strip())
