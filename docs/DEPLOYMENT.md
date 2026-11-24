# VPN Private — Production Deployment & Operation Guide

### 1. Architecture Topology
The production deployment orchestrates 7 isolated containers under Docker Compose:
- **`vpnprivate-postgres`**: PostgreSQL 16 database storing all state, users, ledger, panels, and subscriptions. Internal network only.
- **`vpnprivate-redis`**: In-memory store handling Celery brokers, results, and distributed lock synchronization. Internal network only.
- **`vpnprivate-api`**: FastAPI application handling payment gateway callbacks (e.g. Zarinpal) and health checks.
- **`vpnprivate-bot`**: Aiogram 3 worker running long-polling with Telegram's Bot API servers.
- **`vpnprivate-worker`**: Celery worker executing client provisioning, panel traffic synchronization, and expirations.
- **`vpnprivate-scheduler`**: Celery Beat triggering scheduled maintenance jobs.
- **`vpnprivate-nginx`**: Reverse proxy terminating HTTP/HTTPS, enforcing security headers, proxying `/api/v1/` and `/health`.

---

### 2. Pre-Requisites
1. Linux Virtual Private Server (VPS) with at least 2 vCPUs and 4GB RAM.
2. A registered domain pointing to your server IP (e.g. `api.vpnprivate.com`).
3. Installed Docker & Docker Compose plugin:
   ```bash
   sudo apt-get update
   sudo apt-get install -y ca-certificates curl gnupg
   sudo install -m 0755 -d /etc/apt/keyrings
   curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
   sudo chmod a+r /etc/apt/keyrings/docker.gpg
   echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
   sudo apt-get update
   sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   ```

---

### 3. Step-by-Step Deployment

#### Step 1: Clone Repository
```bash
git clone <repo_url> /opt/vpn-private
cd /opt/vpn-private
```

#### Step 2: Configure Environment (.env)
```bash
cp .env.example .env
nano .env
```
Ensure you set:
- `SECRET_KEY`: A random 32+ character string.
- `ENCRYPTION_KEY`: A valid Fernet 32-byte base64 string (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
- `BOT_TOKEN`: Telegram bot token from @BotFather.
- `ADMIN_TELEGRAM_IDS`: Array of numeric Telegram user IDs of the owners.
- `CARD_NUMBER` & `CARD_HOLDER`: Bank card info for manual Card-to-Card receipts.
- `ZARINPAL_MERCHANT_ID`: Your Zarinpal merchant UUID.

#### Step 3: Build & Launch Containers
```bash
docker compose build --pull
docker compose up -d
docker compose ps
```

#### Step 4: Seed Initial Data
```bash
docker compose exec vpnprivate-api python scripts/seed_data.py
```

#### Step 5: Verify Health
```bash
curl http://localhost/health
```
Expected output:
```json
{"status":"healthy","database":"connected","version":"1.0.0","target_3xui":"v3.11"}
```

---

### 4. Zero-Downtime Safe Upgrades
When updating the platform code:
```bash
git pull origin main
docker compose build vpnprivate-api vpnprivate-bot vpnprivate-worker vpnprivate-scheduler
docker compose up -d --no-deps vpnprivate-api vpnprivate-bot vpnprivate-worker vpnprivate-scheduler
```
*Persistent database volumes (`postgres_data`, `redis_data`) are never destroyed during updates.*

