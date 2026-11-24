"""Saman Electronic Payment (SEP / پرداخت الکترونیک سامان) Integration."""
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


class SEPGateway(BasePaymentGateway):
    """Saman Electronic Payment (SEP - پرداخت الکترونیک سامان / شاپرک) Gateway."""

    TOKEN_URL = "https://sep.shaparak.ir/OnlinePG/OnlinePG"
    VERIFY_URL = "https://sep.shaparak.ir/verifyTxnRandomSessionkey/ipg/VerifyTransaction"

    def __init__(
        self,
        terminal_id: Optional[str] = None,
        merchant_id: Optional[str] = None,
        callback_url: Optional[str] = None,
    ):
        self.terminal_id = str(terminal_id or getattr(settings, "SEP_TERMINAL_ID", "")).strip()
        self.merchant_id = str(merchant_id or getattr(settings, "SEP_MERCHANT_ID", "")).strip()
        self.callback_url = callback_url or f"{settings.SERVER_HOST.rstrip('/')}/api/v1/payments/sep/callback"

    async def request_payment(
        self,
        payment_id: str,
        amount_irr: int,
        callback_url: Optional[str] = None,
        description: str = "پرداخت سفارش VPN Private",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentInitResult:
        """Acquire transaction token from SEP Shaparak."""
        cb_url = callback_url or self.callback_url
        meta = metadata or {}
        tracking_code = meta.get("tracking_code") or f"SEP-{payment_id[:8]}"

        payload = {
            "action": "token",
            "TerminalId": self.terminal_id,
            "Amount": amount_irr,
            "ResNum": tracking_code,
            "RedirectURL": cb_url,
            "CellNumber": str(meta.get("phone", "")),
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post(self.TOKEN_URL, json=payload)
                data = resp.json() if resp.status_code == 200 else {}
                
                # SEP returns status: 1 for success with token
                token = data.get("token") or data.get("Token")
                status = data.get("status")
                error_code = data.get("errorCode") or data.get("ErrorCode", 0)
                error_desc = data.get("errorDesc") or data.get("ErrorDesc", "")

                if (status == 1 or token) and error_code == 0:
                    # Provide direct pay form URL through our server or shaparak
                    redirect_url = f"{settings.SERVER_HOST.rstrip('/')}/api/v1/payments/sep/pay/{payment_id}"
                    return PaymentInitResult(
                        payment_id=payment_id,
                        authority=token,
                        redirect_url=redirect_url,
                        tracking_code=tracking_code,
                        status="PENDING",
                        raw_response=data,
                    )
                else:
                    msg = error_desc or f"خطا در دریافت توکن درگاه سامان (کد خطا: {error_code})"
                    logger.error(f"SEP request error: {msg}")
                    return PaymentInitResult(
                        payment_id=payment_id,
                        status="FAILED",
                        instructions=f"خطا در اتصال به درگاه پرداخت سامان (SEP): {msg}",
                        raw_response=data,
                    )
            except Exception as e:
                logger.error(f"SEP request failed exception: {e}")
                return PaymentInitResult(
                    payment_id=payment_id,
                    status="FAILED",
                    instructions=f"عدم امکان برقراری ارتباط با درگاه سامان (SEP): {e}",
                )

    async def verify_payment(
        self,
        ref_num: str,
        amount_irr: int,
        terminal_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentVerifyResult:
        """Verify transaction with SEP server-to-server."""
        term_id = str(terminal_id or self.terminal_id).strip()
        payload = {
            "RefNum": ref_num,
            "TerminalNumber": term_id,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.post(self.VERIFY_URL, json=payload)
                data = resp.json() if resp.status_code == 200 else {}
                
                # ResultCode: 0 is Success in modern REST, or positive integer == amount_irr
                result_code = data.get("ResultCode")
                is_success = data.get("Success") is True or result_code == 0 or result_code == amount_irr
                
                tx_detail = data.get("TransactionDetail") or {}
                masked_pan = tx_detail.get("MaskedPan") or data.get("StrCardNoMask") or ""
                final_ref = str(tx_detail.get("RefNum") or ref_num)

                if is_success:
                    return PaymentVerifyResult(
                        success=True,
                        amount_irr=amount_irr,
                        reference_id=final_ref,
                        card_pan=masked_pan,
                        raw_response=data,
                    )
                else:
                    err_msg = data.get("ResultDescription") or f"خطای تایید تراکنش سامان (کد: {result_code})"
                    return PaymentVerifyResult(
                        success=False,
                        error_message=err_msg,
                        raw_response=data,
                    )
            except Exception as e:
                logger.error(f"SEP verify failed exception: {e}")
                return PaymentVerifyResult(
                    success=False,
                    error_message=f"خطا در اتصال به سرور تایید تراکنش سامان: {e}",
                )

    def validate_callback(self, params: Dict[str, Any]) -> bool:
        """Validate if SEP callback indicates successful payment by user."""
        state = str(params.get("State", "")).upper()
        status = str(params.get("Status", ""))
        return state == "OK" or status in ("2", "0")
