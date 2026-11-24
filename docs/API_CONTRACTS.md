# VPN Private — Shared Contracts & Interface Specifications

### 1. Enumerations & Status Transitions

#### OrderStatus
- `CREATED`: Initial order record created.
- `AWAITING_PAYMENT`: Awaiting user to proceed to payment.
- `PAYMENT_PENDING`: Payment submitted (e.g. Card-to-Card receipt awaiting admin review, or Gateway checkout initiated).
- `PAID`: Payment successfully captured and verified.
- `PROVISIONING`: Background client allocation underway on 3x-ui panel.
- `ACTIVE`: Client successfully created on panel, subscription delivered.
- `PROVISIONING_FAILED`: Error communicating with panel (alert logged, eligible for safe retry).
- `CANCELLED`: Order cancelled before payment.
- `REFUNDED`: Funds returned to customer wallet.

#### SubscriptionStatus
- `ACTIVE`: Normal operating status on panel.
- `EXPIRED`: Duration reached or traffic quota consumed.
- `DISABLED`: Manually or system disabled on 3x-ui inbound.
- `SUSPENDED`: Suspended by admin or fraud check.

#### PaymentMethod
- `CARD_TO_CARD`
- `ZARINPAL`
- `WALLET`
- `NOWPAYMENTS` (Crypto extensible)

#### UserRole
- `CUSTOMER`
- `RESELLER`
- `SUPPORT`
- `FINANCE`
- `SERVER_ADMIN`
- `SUPER_ADMIN`

---

### 2. Panel Adapter Contract (`ThreeXUIPanelAdapter`)
```python
class ThreeXUIPanelAdapter(ABC):
    @abstractmethod
    async def authenticate(self) -> bool: ...

    @abstractmethod
    async def test_connection(self) -> bool: ...

    @abstractmethod
    async def get_server_status(self) -> Dict[str, Any]: ...

    @abstractmethod
    async def list_inbounds(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    async def get_inbound(self, inbound_id: int) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    async def add_client(self, inbound_id: int, client_spec: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    async def update_client(self, inbound_id: int, client_id: str, client_spec: Dict[str, Any]) -> bool: ...

    @abstractmethod
    async def delete_client(self, inbound_id: int, client_id: str) -> bool: ...

    @abstractmethod
    async def reset_client_traffic(self, inbound_id: int, email: str) -> bool: ...

    @abstractmethod
    async def get_client_traffic(self, email: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def generate_client_url(self, protocol: str, inbound_data: dict, client_data: dict, server_host: str) -> str: ...

    @abstractmethod
    def generate_subscription_url(self, server_host: str, sub_port: int, sub_id: str, use_ssl: bool = False) -> str: ...
```

---

### 3. Payment Gateway Contract (`BasePaymentGateway`)
```python
class BasePaymentGateway(ABC):
    @abstractmethod
    async def create_payment(self, payment_id: str, amount_irr: int, callback_url: str, description: str, metadata: dict) -> PaymentInitResult: ...

    @abstractmethod
    async def verify_payment(self, authority: str, amount_irr: int, metadata: dict) -> PaymentVerifyResult: ...

    @abstractmethod
    async def validate_callback(self, params: dict) -> bool: ...
```

---

### 4. Double-Entry Accounting Invariant
$$\sum \text{Debits} - \sum \text{Credits} = 0 \quad (\text{per transaction group})$$
Every monetary mutation (deposit, purchase, commission, refund) MUST be atomic and balanced.
