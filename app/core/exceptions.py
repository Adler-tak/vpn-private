"""Typed custom exceptions for the platform."""


class VPNPrivateError(Exception):
    """Base exception for all domain errors."""
    def __init__(self, message: str, code: str = "INTERNAL_ERROR"):
        self.message = message
        self.code = code
        super().__init__(self.message)


class PanelError(VPNPrivateError):
    """Error communicating with or executing on a 3x-ui panel."""
    pass


class PanelAuthError(PanelError):
    """Authentication failed with the 3x-ui panel."""
    pass


class InboundNotFoundError(PanelError):
    """Requested inbound was not found on the specified panel."""
    pass


class ProvisioningError(VPNPrivateError):
    """Failure during VPN client provisioning."""
    pass


class PaymentError(VPNPrivateError):
    """Generic payment processing error."""
    pass


class InsufficientFundsError(PaymentError):
    """Wallet balance is insufficient for this transaction."""
    pass


class OrderStateError(VPNPrivateError):
    """Invalid order status transition."""
    pass


class IdempotencyViolationError(VPNPrivateError):
    """Duplicate request detected."""
    pass


class SecurityViolationError(VPNPrivateError):
    """Unauthorized operation or tampered signature."""
    pass
