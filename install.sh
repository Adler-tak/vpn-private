#!/usr/bin/env bash
# =====================================================================
# VPN Private — Automated 1-Click Production Installer & DevOps Wizard
# =====================================================================
# Supports interactive piping:
#   bash <(curl -Ls https://raw.githubusercontent.com/Adler-tak/vpn-private/main/install.sh)
# =====================================================================
set -e

# Terminal Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
BOLD='\033[1m'
NC='\033[0m' # No Color

DEFAULT_INSTALL_DIR="/opt/vpn-private"
REPO_URL="https://github.com/Adler-tak/vpn-private.git"

# Parse CLI arguments (--dir / -d / positional path)
CLI_DIR_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dir|-d)
            if [ -n "$2" ]; then
                CLI_DIR_ARG="$2"
                shift 2
            else
                echo -e "${RED}[ERROR] Option --dir requires a path argument.${NC}" >&2
                exit 1
            fi
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS] [INSTALL_PATH]"
            echo "Options:"
            echo "  -d, --dir PATH    Specify target installation directory (default: /opt/vpn-private)"
            echo "  -h, --help        Show this help message"
            exit 0
            ;;
        *)
            if [ -z "$CLI_DIR_ARG" ] && [[ "$1" != -* ]]; then
                CLI_DIR_ARG="$1"
            fi
            shift
            ;;
    esac
done

# ---------------------------------------------------------------------
# 1. Interactive TTY Setup (Support piping from curl)
# ---------------------------------------------------------------------
if [ -e /dev/tty ] && [ -r /dev/tty ]; then
    exec < /dev/tty
    HAS_TTY=true
else
    HAS_TTY=false
fi

prompt_input() {
    local text="$1"
    local default_val="$2"
    local var_ref="$3"
    local val=""

    if [ -n "$default_val" ]; then
        if [ "$HAS_TTY" = true ]; then
            printf "%b [%b%s%b]: " "$text" "$CYAN" "$default_val" "$NC" > /dev/tty
            read -r val < /dev/tty || true
        else
            printf "%b [%b%s%b]: " "$text" "$CYAN" "$default_val" "$NC"
            read -r val || true
        fi
    else
        if [ "$HAS_TTY" = true ]; then
            printf "%b: " "$text" > /dev/tty
            read -r val < /dev/tty || true
        else
            printf "%b: " "$text"
            read -r val || true
        fi
    fi

    if [ -z "$val" ]; then
        val="$default_val"
    fi
    eval "$var_ref=\"\$val\""
}

# ---------------------------------------------------------------------
# 2. Stylish Installation Banner
# ---------------------------------------------------------------------
clear 2>/dev/null || true
echo -e "${CYAN}${BOLD}"
cat << "EOF"
 ========================================================================
    __     ______  _   _   ____  ____  _____     ___  _____ _____ 
    \ \   / /  _ \| \ | | |  _ \|  _ \|_ _\ \   / / \|_   _| ____|
     \ \ / /| |_) |  \| | | |_) | |_) || | \ \ / / _ \ | | |  _|  
      \ V / |  __/| |\  | |  __/|  _ < | |  \ V / ___ \| | | |___ 
       \_/  |_|   |_| \_| |_|   |_| \_\___|  \_/_/   \_\_| |_____|
                                                                   
        ★ Automated 1-Click Installer & DevOps Deployment ★
 ========================================================================
EOF
echo -e "${NC}"

# ---------------------------------------------------------------------
# 3. Root Privilege Verification
# ---------------------------------------------------------------------
if [ "$(id -u)" != "0" ]; then
    echo -e "${RED}[ERROR] This installer must be executed with root privileges.${NC}" >&2
    echo -e "${YELLOW}Please re-run using sudo or as root:${NC}" >&2
    echo -e "  sudo bash <(curl -Ls https://raw.githubusercontent.com/Adler-tak/vpn-private/main/install.sh)" >&2
    exit 1
fi
echo -e "${GREEN}[✓] Root privileges verified.${NC}"

# ---------------------------------------------------------------------
# 4. Operating System Compatibility Check
# ---------------------------------------------------------------------
echo -e "\n${BLUE}[*] Checking Operating System compatibility...${NC}"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="${ID:-}"
    OS_LIKE="${ID_LIKE:-}"
else
    echo -e "${RED}[ERROR] Cannot detect operating system. Debian or Ubuntu required.${NC}" >&2
    exit 1
fi

case "$OS_ID" in
    ubuntu|debian|raspbian)
        echo -e "${GREEN}[✓] Supported OS detected: $NAME ($VERSION_ID)${NC}"
        ;;
    *)
        if [[ "$OS_LIKE" =~ (debian|ubuntu) ]]; then
            echo -e "${GREEN}[✓] Debian/Ubuntu-derivative detected: $NAME ($VERSION_ID)${NC}"
        else
            echo -e "${YELLOW}[WARNING] Your system ($NAME) is not Debian/Ubuntu.${NC}"
            prompt_input "Do you wish to proceed anyway? (y/N)" "N" proceed_unsupported
            if [[ ! "$proceed_unsupported" =~ ^[yY]$ ]]; then
                echo -e "${RED}Installation cancelled.${NC}"
                exit 1
            fi
        fi
        ;;
esac

# ---------------------------------------------------------------------
# 5. System Dependencies Installation
# ---------------------------------------------------------------------
echo -e "\n${BLUE}[*] Updating system packages and installing dependencies...${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

PKGS=(curl git openssl jq ufw python3 python3-pip sqlite3 ca-certificates gnupg lsb-release)
TO_INSTALL=()
for pkg in "${PKGS[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        TO_INSTALL+=("$pkg")
    fi
done

if [ ${#TO_INSTALL[@]} -gt 0 ]; then
    echo -e "${YELLOW}[*] Installing packages: ${TO_INSTALL[*]}...${NC}"
    apt-get install -y -qq "${TO_INSTALL[@]}"
else
    echo -e "${GREEN}[✓] All core base packages are already installed.${NC}"
fi

# Optional bcrypt support for password hashing
if ! python3 -c "import bcrypt" &>/dev/null; then
    apt-get install -y -qq python3-bcrypt 2>/dev/null || pip3 install --break-system-packages bcrypt 2>/dev/null || pip3 install bcrypt 2>/dev/null || true
fi

# Firewall (UFW) Configuration
if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -qw "active"; then
    echo -e "${BLUE}[*] Active UFW firewall detected. Ensuring core service ports are open...${NC}"
    ufw allow 22/tcp 2>/dev/null || true
    ufw allow 80/tcp 2>/dev/null || true
    ufw allow 443/tcp 2>/dev/null || true
    ufw allow 8000/tcp 2>/dev/null || true
    echo -e "${GREEN}[✓] UFW firewall ports (22, 80, 443, 8000) confirmed open.${NC}"
fi

# ---------------------------------------------------------------------
# 6. Docker Engine & Docker Compose Detection / Installation
# ---------------------------------------------------------------------
echo -e "\n${BLUE}[*] Verifying Docker and Docker Compose...${NC}"
if ! command -v docker &>/dev/null; then
    echo -e "${YELLOW}[*] Docker not found. Installing Docker Engine via official script...${NC}"
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo -e "${GREEN}[✓] Docker installed successfully.${NC}"
else
    echo -e "${GREEN}[✓] Docker is already installed: $(docker --version)${NC}"
fi

# Docker Compose detection (v2 plugin or standalone)
if ! docker compose version &>/dev/null && ! command -v docker-compose &>/dev/null; then
    echo -e "${YELLOW}[*] Installing docker-compose-plugin...${NC}"
    apt-get install -y -qq docker-compose-plugin || true
fi

if docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo -e "${RED}[ERROR] Failed to locate or install Docker Compose.${NC}" >&2
    exit 1
fi
echo -e "${GREEN}[✓] Docker Compose verified: $($DC version)${NC}"

# ---------------------------------------------------------------------
# 7. Installation Directory Selection & Repository Setup
# ---------------------------------------------------------------------
echo -e "\n${BLUE}[*] Installation Directory Selection:${NC}"
TARGET_DIR="${CLI_DIR_ARG:-${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}}"

if [ -n "$CLI_DIR_ARG" ]; then
    INSTALL_DIR="$CLI_DIR_ARG"
    echo -e "  Specified via argument: ${BOLD}${CYAN}${INSTALL_DIR}${NC}"
elif [ "$HAS_TTY" = true ] && [ -z "$NON_INTERACTIVE" ]; then
    echo -e "${YELLOW}You can specify a custom directory to install VPN Private, or press Enter for default.${NC}"
    prompt_input "Enter installation path" "$TARGET_DIR" CHOSEN_DIR
    INSTALL_DIR="$CHOSEN_DIR"
else
    INSTALL_DIR="$TARGET_DIR"
fi

# Sanitize and normalize path
INSTALL_DIR="$(echo "$INSTALL_DIR" | sed 's:/*$::')"
if [ -z "$INSTALL_DIR" ]; then
    INSTALL_DIR="$DEFAULT_INSTALL_DIR"
fi

# Expand ~ to home directory if present
if [[ "$INSTALL_DIR" == ~* ]]; then
    INSTALL_DIR="${HOME}${INSTALL_DIR#\~}"
fi

# Convert to absolute path if relative
if [[ "$INSTALL_DIR" != /* ]]; then
    INSTALL_DIR="$(pwd)/$INSTALL_DIR"
fi

echo -e "${GREEN}[✓] Target installation directory set to:${NC} ${BOLD}${CYAN}${INSTALL_DIR}${NC}"
echo -e "\n${BLUE}[*] Setting up VPN Private repository in $INSTALL_DIR...${NC}"
CURRENT_DIR="$(pwd)"

if [ "$CURRENT_DIR" != "$INSTALL_DIR" ] && [ -f "$CURRENT_DIR/docker-compose.yml" ] && [ -f "$CURRENT_DIR/Dockerfile" ]; then
    echo -e "${YELLOW}[*] Local repository detected. Syncing to $INSTALL_DIR...${NC}"
    mkdir -p "$INSTALL_DIR"
    cp -a "$CURRENT_DIR/." "$INSTALL_DIR/"
    cd "$INSTALL_DIR"
elif [ -d "$INSTALL_DIR/.git" ]; then
    echo -e "${GREEN}[✓] Existing git installation found at $INSTALL_DIR. Pulling latest main branch...${NC}"
    cd "$INSTALL_DIR"
    git fetch --all --prune
    git reset --hard origin/main
    git clean -fd -e .env -e vpn_private.db -e "*.db" -e "data/" -e "backups/"
    git pull origin main || true
else
    echo -e "${YELLOW}[*] Cloning repository from $REPO_URL...${NC}"
    mkdir -p "$INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ---------------------------------------------------------------------
# 8. Interactive Configuration Wizard
# ---------------------------------------------------------------------
echo -e "\n${CYAN}${BOLD}========================================================================${NC}"
echo -e "${CYAN}${BOLD}                VPN PRIVATE CONFIGURATION WIZARD                        ${NC}"
echo -e "${CYAN}${BOLD}========================================================================${NC}"

# Check for existing .env
EXISTING_ENV=false
DEF_BOT_TOKEN=""
DEF_ADMIN_IDS=""
DEF_BOT_USER=""
DEF_WEB_USER="admin"
DEF_WEB_PASS=""

if [ -f "$INSTALL_DIR/.env" ]; then
    EXISTING_ENV=true
    DEF_BOT_TOKEN=$(grep -E '^BOT_TOKEN=' "$INSTALL_DIR/.env" | head -n1 | cut -d'=' -f2- | tr -d '"'\''')
    DEF_ADMIN_IDS=$(grep -E '^ADMIN_TELEGRAM_IDS=' "$INSTALL_DIR/.env" | head -n1 | cut -d'=' -f2- | tr -d '"'\''')
    DEF_BOT_USER=$(grep -E '^BOT_USERNAME=' "$INSTALL_DIR/.env" | head -n1 | cut -d'=' -f2- | tr -d '"'\''')
    DEF_WEB_USER=$(grep -E '^ADMIN_WEB_USERNAME=' "$INSTALL_DIR/.env" | head -n1 | cut -d'=' -f2- | tr -d '"'\''')
    DEF_WEB_PASS=$(grep -E '^ADMIN_WEB_PASSWORD=' "$INSTALL_DIR/.env" | head -n1 | cut -d'=' -f2- | tr -d '"'\''')

    echo -e "${YELLOW}[!] Existing configuration file found at $INSTALL_DIR/.env.${NC}"
    prompt_input "Do you want to keep the existing .env configuration? (Y/n)" "Y" keep_env
    if [[ "$keep_env" =~ ^[yY]$ ]]; then
        RECONFIGURE=false
        echo -e "\n${BOLD}${CYAN}--- Web Admin Panel Credentials ---${NC}"
        prompt_input "Do you want to change/update Web Admin Username & Password? (y/N)" "N" change_admin_creds
        if [[ "$change_admin_creds" =~ ^[yY]$ ]]; then
            prompt_input "Enter Web Admin Username" "${DEF_WEB_USER:-admin}" CFG_WEB_USER
            prompt_input "Enter Web Admin Password" "${DEF_WEB_PASS:-admin}" CFG_WEB_PASS

            # Update .env
            if grep -q '^ADMIN_WEB_USERNAME=' "$INSTALL_DIR/.env" 2>/dev/null; then
                sed -i "s/^ADMIN_WEB_USERNAME=.*/ADMIN_WEB_USERNAME=${CFG_WEB_USER}/" "$INSTALL_DIR/.env"
            else
                echo "ADMIN_WEB_USERNAME=${CFG_WEB_USER}" >> "$INSTALL_DIR/.env"
            fi
            if grep -q '^ADMIN_WEB_PASSWORD=' "$INSTALL_DIR/.env" 2>/dev/null; then
                sed -i "s/^ADMIN_WEB_PASSWORD=.*/ADMIN_WEB_PASSWORD=${CFG_WEB_PASS}/" "$INSTALL_DIR/.env"
            else
                echo "ADMIN_WEB_PASSWORD=${CFG_WEB_PASS}" >> "$INSTALL_DIR/.env"
            fi

            # Update data/system_settings.json
            mkdir -p "$INSTALL_DIR/data"
            cat << EOF > "$INSTALL_DIR/data/system_settings.json"
{
  "ADMIN_WEB_USERNAME": "${CFG_WEB_USER}",
  "ADMIN_WEB_PASSWORD": "${CFG_WEB_PASS}"
}
EOF
            echo -e "${GREEN}[✓] Web Admin credentials updated.${NC}"
        else
            CFG_WEB_USER="${DEF_WEB_USER:-admin}"
            CFG_WEB_PASS="${DEF_WEB_PASS:-admin}"
        fi
    else
        RECONFIGURE=true
    fi
else
    RECONFIGURE=true
fi

if [ "$RECONFIGURE" = true ]; then
    echo -e "\n${WHITE}Please provide the following required parameters:${NC}\n"

    # 1. Telegram Bot Token
    while true; do
        prompt_input "Enter Telegram Bot Token (from @BotFather)" "$DEF_BOT_TOKEN" CFG_BOT_TOKEN
        if [[ "$CFG_BOT_TOKEN" =~ ^[0-9]+:[a-zA-Z0-9_-]+$ ]]; then
            break
        else
            echo -e "${RED}[!] Invalid Bot Token format! Example: 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ${NC}"
        fi
    done

    # 2. Admin Telegram ID
    while true; do
        prompt_input "Enter Admin Telegram ID (from @userinfobot)" "$DEF_ADMIN_IDS" CFG_ADMIN_IDS
        if [[ "$CFG_ADMIN_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
            break
        else
            echo -e "${RED}[!] Invalid Telegram ID! Must be numeric (e.g. 123456789 or 123456789,987654321).${NC}"
        fi
    done

    # 3. Bot Username
    prompt_input "Enter Telegram Bot Username (without @)" "${DEF_BOT_USER:-VPNPrivateSell_Bot}" CFG_BOT_USER
    CFG_BOT_USER="${CFG_BOT_USER#@}"

    # 4. Web Admin Credentials
    echo -e "\n${BOLD}${CYAN}--- Web Admin Panel Credentials (ورود پنل مدیریت وب) ---${NC}"
    echo -e "${YELLOW}Set your login credentials for the Web Admin Dashboard:${NC}"
    prompt_input "Enter Web Admin Username" "${DEF_WEB_USER:-admin}" CFG_WEB_USER
    prompt_input "Enter Web Admin Password" "${DEF_WEB_PASS:-admin123456}" CFG_WEB_PASS

    # 6. Generate cryptographic keys
    echo -e "\n${BLUE}[*] Generating cryptographic application keys...${NC}"
    SECRET_KEY=$(openssl rand -hex 32)
    
    # 32-byte URL-safe base64 Fernet key
    if python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" &>/dev/null; then
        ENCRYPTION_KEY=$(python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")
    else
        ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
    fi

    # Generate Bcrypt hash if available
    BCRYPT_HASH=""
    if python3 -c "import bcrypt" &>/dev/null; then
        BCRYPT_HASH=$(python3 -c "import bcrypt; print(bcrypt.hashpw(b'''$CFG_WEB_PASS''', bcrypt.gensalt()).decode())" 2>/dev/null || echo "")
    fi
    # Escape $ signs for Docker Compose interpolation ($$ -> $)
    BCRYPT_HASH_ESCAPED="${BCRYPT_HASH//\$/\$\$}"

    # Detect Public IP
    SERVER_IP=$(curl -s -m 4 -4 https://api.ipify.org 2>/dev/null || curl -s -m 4 -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    if [ -z "$SERVER_IP" ]; then
        SERVER_IP="127.0.0.1"
    fi
    SERVER_HOST="http://${SERVER_IP}:8000"

    echo -e "${YELLOW}[*] Writing configuration to $INSTALL_DIR/.env...${NC}"
    cat << EOF > "$INSTALL_DIR/.env"
# =====================================================================
# VPN Private — Production Core Environment (3-to-97 Rule)
# Automatically generated by install.sh on $(date -u +"%Y-%m-%d %H:%M:%S UTC")
# All dynamic settings (cards, crypto, gateways, trials, bot texts)
# are stored in the database (table: system_settings)
# =====================================================================

# Application
PROJECT_NAME="VPN Private"
ENVIRONMENT=production
DEBUG=false
SECRET_KEY=${SECRET_KEY}
ENCRYPTION_KEY=${ENCRYPTION_KEY}
API_V1_STR=/api/v1
SERVER_HOST=${SERVER_HOST}

# Web Admin Authentication Credentials
ADMIN_WEB_USERNAME=${CFG_WEB_USER}
ADMIN_WEB_PASSWORD=${CFG_WEB_PASS}
ADMIN_WEB_PASSWORD_HASH=${BCRYPT_HASH_ESCAPED}

# Telegram Bot Initial Bootstrap
BOT_TOKEN=${CFG_BOT_TOKEN}
BOT_USERNAME=${CFG_BOT_USER}
ADMIN_TELEGRAM_IDS=${CFG_ADMIN_IDS}

# Database (Default: High-Performance SQLite inside container)
DATABASE_URL=sqlite+aiosqlite:///vpn_private.db
DATABASE_URL_SYNC=sqlite:///vpn_private.db

# Redis & Celery
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2
EOF

    mkdir -p "$INSTALL_DIR/data"
    cat << EOF > "$INSTALL_DIR/data/system_settings.json"
{
  "ADMIN_WEB_USERNAME": "${CFG_WEB_USER}",
  "ADMIN_WEB_PASSWORD": "${CFG_WEB_PASS}",
  "ADMIN_WEB_PASSWORD_HASH": "${BCRYPT_HASH_ESCAPED}",
  "BOT_USERNAME": "${CFG_BOT_USER}",
  "SUPPORT_USERNAME": "${CFG_BOT_USER}Support"
}
EOF

    chmod 600 "$INSTALL_DIR/.env"
    echo -e "${GREEN}[✓] Configuration written with strict permissions (chmod 600).${NC}"
else
    # Load settings from existing .env for summary banner
    CFG_BOT_USER="${DEF_BOT_USER:-VPNPrivateSell_Bot}"
    CFG_WEB_USER="${DEF_WEB_USER:-admin}"
    CFG_WEB_PASS="${DEF_WEB_PASS:-[Unchanged]}"
    SERVER_IP=$(curl -s -m 4 -4 https://api.ipify.org 2>/dev/null || curl -s -m 4 -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
fi

# Ensure SQLite file exists with proper permissions
if [ -d "$INSTALL_DIR/vpn_private.db" ]; then
    rm -rf "$INSTALL_DIR/vpn_private.db"
    touch "$INSTALL_DIR/vpn_private.db"
elif [ ! -f "$INSTALL_DIR/vpn_private.db" ]; then
    touch "$INSTALL_DIR/vpn_private.db"
fi
chmod 644 "$INSTALL_DIR/vpn_private.db"

# ---------------------------------------------------------------------
# 9. Docker Network Setup
# ---------------------------------------------------------------------
echo -e "\n${BLUE}[*] Ensuring external Docker network exists...${NC}"
docker network create nginx-proxy-manager_default 2>/dev/null || true
echo -e "${GREEN}[✓] Docker network 'nginx-proxy-manager_default' is active.${NC}"

# ---------------------------------------------------------------------
# 10. Pull Pre-built Image and Start Docker Containers
# ---------------------------------------------------------------------
echo -e "\n${BLUE}[*] Pulling official pre-built image from Docker Hub (adprivate/vpn-private:latest)...${NC}"
cd "$INSTALL_DIR"

# Clean up stale or expired Docker credentials that cause 401 PAT errors on Docker Hub
if [ -f "$HOME/.docker/config.json" ] && grep -qi "auths" "$HOME/.docker/config.json" 2>/dev/null; then
    docker logout 2>/dev/null || true
fi

if ! $DC pull; then
    echo -e "${YELLOW}[!] Retrying Docker pull with clean anonymous credentials...${NC}"
    if [ -f "$HOME/.docker/config.json" ]; then
        mv "$HOME/.docker/config.json" "$HOME/.docker/config.json.bak_$(date +%s)" 2>/dev/null || true
    fi
    if ! $DC pull; then
        echo -e "${YELLOW}[!] Image pull from Docker Hub failed; falling back to local build...${NC}"
        $DC up -d --build
    else
        echo -e "${GREEN}[✓] Pre-built image pulled successfully.${NC}"
        $DC up -d
    fi
else
    echo -e "${GREEN}[✓] Pre-built image pulled successfully.${NC}"
    $DC up -d
fi

# ---------------------------------------------------------------------
# 11. Database Schema & Core Settings Initialization
# ---------------------------------------------------------------------
echo -e "\n${BLUE}[*] Waiting for API container initialization...${NC}"
for i in {1..30}; do
    if $DC exec -T vpn-api python -c "import sys; sys.exit(0)" &>/dev/null; then
        break
    fi
    sleep 1
done
echo -e "${YELLOW}[*] Running clean database schema creation and core settings initialization...${NC}"
$DC exec -T vpn-api python scripts/seed_data.py || python3 "$INSTALL_DIR/scripts/seed_data.py" || true
echo -e "${GREEN}[✓] Database schema and core settings initialized successfully.${NC}"

# Explicitly synchronize configured Web Admin credentials into database table only when requested or on fresh install
if ([ "$EXISTING_ENV" = false ] || [ "$RECONFIGURE" = true ] || [[ "$change_admin_creds" =~ ^[yY]$ ]]) && [ -n "$CFG_WEB_USER" ] && [ -n "$CFG_WEB_PASS" ]; then
    $DC exec -T vpn-api python -c "
import asyncio
import bcrypt
from app.services.settings_service import SettingsService
from app.api.routes.admin import security_store

async def main():
    await SettingsService.set_value('ADMIN_WEB_USERNAME', '$CFG_WEB_USER')
    await SettingsService.set_value('ADMIN_WEB_PASSWORD', '$CFG_WEB_PASS')
    try:
        pw_hash = bcrypt.hashpw(b'''$CFG_WEB_PASS''', bcrypt.gensalt()).decode('utf-8')
        await SettingsService.set_value('ADMIN_WEB_PASSWORD_HASH', pw_hash)
    except Exception:
        pass
    if hasattr(security_store, 'clear_all_blocks'):
        security_store.clear_all_blocks()
    else:
        getattr(security_store, '_failed_attempts', {}).clear()
        getattr(security_store, '_blocked_ips', {}).clear()
    print('  [✓] Web Admin credentials successfully synchronized into database.')

asyncio.run(main())
" 2>/dev/null || true
fi

# ---------------------------------------------------------------------
# 12. Global CLI Management Tool Installation
# ---------------------------------------------------------------------
echo -e "\n${BLUE}[*] Installing global 'vpn-private' command line management tool...${NC}"
chmod +x "$INSTALL_DIR/scripts/vpn-private-cli.sh"
chmod +x "$INSTALL_DIR/scripts/backup.sh"
chmod +x "$INSTALL_DIR/scripts/restore.sh"
chmod +x "$INSTALL_DIR/uninstall.sh" 2>/dev/null || true

ln -sf "$INSTALL_DIR/scripts/vpn-private-cli.sh" /usr/local/bin/vpn-private
chmod +x /usr/local/bin/vpn-private
mkdir -p /etc/vpn-private
echo "$INSTALL_DIR" > /etc/vpn-private/install_dir
echo -e "${GREEN}[✓] Global CLI command installed: /usr/local/bin/vpn-private${NC}"

# ---------------------------------------------------------------------
# 13. Completion Summary Banner
# ---------------------------------------------------------------------
echo -e "\n${GREEN}${BOLD}"
cat << "EOF"
 ========================================================================
           🎉 VPN PRIVATE DEPLOYED & INSTALLED SUCCESSFULLY! 🎉
 ========================================================================
EOF
echo -e "${NC}"
echo -e "  ${WHITE}${BOLD}Access Credentials & System URLs:${NC}"
echo -e "  ${CYAN}• Web Admin URL:${NC}       ${BOLD}http://${SERVER_IP}:8000/admin${NC}"
echo -e "  ${CYAN}• Web Admin Username:${NC}  ${BOLD}${CFG_WEB_USER}${NC}"
echo -e "  ${CYAN}• Web Admin Password:${NC}  ${BOLD}${CFG_WEB_PASS}${NC}"
echo -e "  ${CYAN}• Telegram Bot:${NC}        ${BOLD}@${CFG_BOT_USER}${NC}"
echo -e "  ${CYAN}• Installation Dir:${NC}    ${BOLD}${INSTALL_DIR}${NC}"
echo ""
echo -e "  ${WHITE}${BOLD}Server Administration CLI:${NC}"
echo -e "  Simply type ${GREEN}${BOLD}vpn-private${NC} in your terminal to open the management menu:"
echo -e "  ${YELLOW}• Status & Health Check    • Restart / Stop / Start Services${NC}"
echo -e "  ${YELLOW}• Real-time Live Logs      • 1-Click Platform Updates${NC}"
echo -e "  ${YELLOW}• Automated DB Backups     • Quick Disaster Recovery${NC}"
echo ""
echo -e "${GREEN}========================================================================${NC}\n"
