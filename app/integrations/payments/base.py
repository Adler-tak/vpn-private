"""Base payment gateway interface and schemas."""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from pydantic import BaseModel


class PaymentInitResult(BaseModel):
    payment_id: str
    authority: str = ""
    redirect_url: str = ""
    tracking_code: str = ""
    instructions: Optional[str] = None
    status: str = "PENDING"
    raw_response: Dict[str, Any] = {}


class PaymentVerifyResult(BaseModel):
    success: bool
    amount_irr: int = 0
    reference_id: str = ""
    card_pan: str = ""
    fee_irr: int = 0
    error_message: Optional[str] = None
    raw_response: Dict[str, Any] = {}


class BasePaymentGateway(ABC):
    """Abstract base class for all payment gateways."""

    @abstractmethod
    async def request_payment(
        self,
        payment_id: str,
        amount_irr: int,
        callback_url: str,
        description: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentInitResult:
        """Initiate payment with gateway."""
        pass

    @abstractmethod
    async def verify_payment(
        self,
        authority: str,
        amount_irr: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PaymentVerifyResult:
        """Verify payment with gateway server-to-server."""
        pass

    @abstractmethod
    def validate_callback(self, params: Dict[str, Any]) -> bool:
        """Validate signature or incoming callback status."""
        pass
