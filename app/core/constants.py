"""Core constants and enums for VPN Private."""
from enum import Enum, StrEnum


class UserRole(StrEnum):
    CUSTOMER = "customer"
    RESELLER = "reseller"
    SUPPORT = "support"
    FINANCE = "finance"
    SERVER_ADMIN = "server_admin"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    PROVISIONING_FAILED = "PROVISIONING_FAILED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


class SubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    DISABLED = "DISABLED"
    SUSPENDED = "SUSPENDED"
    PENDING_PROVISION = "PENDING_PROVISION"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    PAID = "PAID"
    REJECTED = "REJECTED"
    REFUNDED = "REFUNDED"
    EXPIRED = "EXPIRED"


class PaymentMethod(StrEnum):
    CARD_TO_CARD = "CARD_TO_CARD"
    ZARINPAL = "ZARINPAL"
    SEP = "SEP"
    IDPAY = "IDPAY"
    WALLET = "WALLET"
    NOWPAYMENTS = "NOWPAYMENTS"
    CRYPTO = "CRYPTO"
    PERFECT_MONEY = "PERFECT_MONEY"
    STRIPE = "STRIPE"
    VOUCHER = "VOUCHER"


class ProtocolType(StrEnum):
    VLESS = "vless"
    VMESS = "vmess"
    TROJAN = "trojan"
    SHADOWSOCKS = "shadowsocks"
    WIREGUARD = "wireguard"
    AMNEZIAWG = "amneziawg"
    OPENVPN = "openvpn"

    @classmethod
    def from_str(cls, value: str) -> "ProtocolType":
        """Case-insensitive and whitespace-tolerant ProtocolType parser with aliases."""
        if not value or not isinstance(value, str):
            raise ValueError(f"Invalid protocol type: {value}")
        val = value.strip().lower()
        alias_map = {
            "wg": cls.WIREGUARD,
            "awg": cls.AMNEZIAWG,
            "ovpn": cls.OPENVPN,
            "ss": cls.SHADOWSOCKS,
        }
        if val in alias_map:
            return alias_map[val]
        for member in cls:
            if member.value == val:
                return member
        raise ValueError(f"Unknown ProtocolType: '{value}'")


class PanelType(StrEnum):
    THREE_X_UI = "3x-ui"
    VPN_UI = "vpn-ui"


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_FOR_CUSTOMER = "WAITING_FOR_CUSTOMER"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class TicketPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Currency(StrEnum):
    IRR = "IRR"
    TOMAN = "TOMAN"


# Canonical Standard Accounts for Double Entry Accounting
ACCOUNT_CASH_GATEWAY = "1010"       # Asset: Real money in bank / gateway clearing
ACCOUNT_CUSTOMER_WALLETS = "2010"   # Liability: Money owed to customers held in system wallets
ACCOUNT_VPN_REVENUE = "4010"        # Revenue: Earned income from VPN subscription sales
ACCOUNT_GATEWAY_FEES = "5010"       # Expense: Transaction processing fees paid to Zarinpal / banks
ACCOUNT_AFFILIATE_EXPENSE = "6010"  # Expense: Commissions paid to resellers and referral partners
