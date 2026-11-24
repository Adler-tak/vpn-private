# VPN Private — Enterprise Architecture Document
## Multi-Agent Coordinated Blueprint

### 1. System Vision
VPN Private is an enterprise-grade automated sales, panel provisioning, multi-server allocation, and financial accounting platform for commercial VPN operations. It interfaces with Telegram Bot (Aiogram 3), FastAPI for webhooks/API, PostgreSQL (SQLAlchemy 2 async), Redis (cache/locks), Celery (asynchronous tasks & sync), and manages multiple independent 3x-ui v3.11 panels.

---

### 2. Multi-Agent Teamwork Structure
The development and maintenance of VPN Private is executed by 8 specialist agents:

| Agent # | Role | Primary Responsibilities |
| :--- | :--- | :--- |
| **Agent 1** | **Lead Architect & Orchestrator** | Architecture governance, cross-agent coordination, contract verification, integration oversight. |
| **Agent 2** | **3x-ui Integration Specialist** | v3.11 adapter (`ThreeXUIPanelAdapter`), inbounds, client lifecycle, config link generation, sync engine. |
| **Agent 3** | **Telegram Bot Developer** | Aiogram 3 Persian UI, keyboards, customer flows, FSM, Admin dashboard, Support UI. |
| **Agent 4** | **Payment & Financial Systems** | Card-to-card workflow, Zarinpal gateway, Wallet, double-entry accounting ledger, idempotency. |
| **Agent 5** | **Backend & Database Engineer** | Async SQLAlchemy 2 models, Alembic migrations, Repositories, FastAPI routes, Redis connection pool. |
| **Agent 6** | **DevOps & Infrastructure** | Dockerfile, docker-compose, Nginx, Celery workers, Celery Beat, backup/restore scripts, health. |
| **Agent 7** | **Security & QA Engineer** | AES-GCM credential encryption, RBAC checks, rate limiting, SQL injection defense, Pytest test suite. |
| **Agent 8** | **Business Logic & Product** | Dynamic plans, server allocation engine, order state machine, subscriptions, reseller/referral rules. |

---

### 3. Layered Modular Monolith Architecture
```
┌────────────────────────────────────────────────────────┐
│                   Customer & Admin                     │
│               Telegram Bot (Aiogram 3)                 │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────┴─────────────────────────────┐
│                 FastAPI API & Webhooks                 │
│         (Payment Callbacks, Health, Management)        │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│                 Core Business Modules                  │
│  [Products] [Orders] [Subscriptions] [Payments]        │
│  [Wallets]  [Accounting] [Server Allocator] [Tickets]  │
└─────────────┬────────────────────────────┬─────────────┘
              │                            │
┌─────────────▼──────────────┐ ┌───────────▼─────────────┐
│  3x-ui v3.11 Panel Adapter │ │ Celery Background Sync  │
│  (Multi-Panel, Inbounds,   │ │ (Traffic Sync, Expiry,  │
│   Client Lifecycle, VLESS) │ │  Notifications, Backup) │
└─────────────┬──────────────┘ └───────────┬─────────────┘
              │                            │
┌─────────────▼────────────────────────────▼─────────────┐
│             PostgreSQL 16 & Redis 7.2 Cache            │
│         (Double-entry Ledger, State Persistence)       │
└────────────────────────────────────────────────────────┘
```

---

### 4. Monetary Policy & Double-Entry Accounting
1. **Canonical Currency:** Iranian Rial (`IRR`) as 64-bit integer (`BigInteger`).
2. **Display Currency:** Iranian Toman (`1 Toman = 10 IRR`).
3. **Double-Entry Principle:** Every financial change creates balanced debit and credit entries.
   - Account 1010: Cash / Gateway Clearing (Asset)
   - Account 2010: Customer Wallets (Liability)
   - Account 4010: VPN Sales Revenue (Revenue)
   - Account 5010: Payment Gateway Fees (Expense)
   - Account 6010: Reseller Commissions (Expense)

---

### 5. Multi-Panel 3x-ui v3.11 Protocol Compatibility
The system connects to multiple panels running 3x-ui v3.11 using session cookie authentication or API bearer tokens.
Supported protocols:
- `VLESS` (XTLS-Reality, TCP, WebSocket, gRPC)
- `VMess` (TCP, WebSocket, gRPC)
- `Trojan` (TLS, gRPC, WebSocket)
- `Shadowsocks` (2022 / AEAD ciphers)
Client links (`vless://...`, `vmess://...`, `trojan://...`, `ss://...`) and subscription links (`/sub/{subId}`) with QR codes are generated directly and validated.
