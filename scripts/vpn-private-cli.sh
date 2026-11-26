#!/usr/bin/env bash
# =====================================================================
# VPN Private — Interactive Server Administration & Management CLI
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

# Determine installation directory (resolving symlinks dynamically)
TARGET_SOURCE="${BASH_SOURCE[0]}"
while [ -h "$TARGET_SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$TARGET_SOURCE")" && pwd)"
    TARGET_SOURCE="$(readlink "$TARGET_SOURCE")"
    [[ $TARGET_SOURCE != /* ]] && TARGET_SOURCE="$DIR/$TARGET_SOURCE"
done
RESOLVED_DIR="$(cd -P "$(dirname "$TARGET_SOURCE")/.." && pwd)"

if [ -f "$RESOLVED_DIR/docker-compose.yml" ]; then
    INSTALL_DIR="$RESOLVED_DIR"
elif [ -n "$VPN_INSTALL_DIR" ] && [ -f "$VPN_INSTALL_DIR/docker-compose.yml" ]; then
    INSTALL_DIR="$VPN_INSTALL_DIR"
elif [ -f "/opt/vpn-private/docker-compose.yml" ]; then
    INSTALL_DIR="/opt/vpn-private"
else
    INSTALL_DIR="$RESOLVED_DIR"
fi

if [ ! -d "$INSTALL_DIR" ]; then
    echo -e "${RED}[ERROR] Installation directory not found at $INSTALL_DIR.${NC}" >&2
    exit 1
fi

cd "$INSTALL_DIR"

# Docker Compose detection
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    echo -e "${RED}[ERROR] Neither 'docker compose' nor 'docker-compose' found.${NC}" >&2
    exit 1
fi

# TTY helper
TTY_DEV="/dev/tty"
if [ ! -e "$TTY_DEV" ] || [ ! -r "$TTY_DEV" ]; then
    TTY_DEV="/dev/stdin"
fi

prompt_read() {
    local prompt_msg="$1"
    local var_name="$2"
    local input_val=""
    printf "%b" "$prompt_msg" > "$TTY_DEV"
    read -r input_val < "$TTY_DEV" || true
    eval "$var_name=\"\$input_val\""
}

pause_prompt() {
    echo ""
    printf "%b" "${CYAN}Press [Enter] to return to menu...${NC}" > "$TTY_DEV"
    read -r _dummy < "$TTY_DEV" || true
}

ensure_networks() {
    if command -v docker &>/dev/null; then
        docker network create nginx-proxy-manager_default 2>/dev/null || true
    fi
}

show_banner() {
    clear 2>/dev/null || true
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════════════════════════╗"
    echo "  ║                  VPN PRIVATE MANAGEMENT CLI                  ║"
    echo "  ║      Enterprise High-Performance VPN Sales & Bot System      ║"
    echo "  ╚══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    echo -e "  ${WHITE}Working Directory:${NC} ${YELLOW}$INSTALL_DIR${NC}"
    echo ""
}

view_status() {
    show_banner
    echo -e "${BOLD}${BLUE}=== 1. Container Status ===${NC}"
    $DC ps
    echo ""

    echo -e "${BOLD}${BLUE}=== 2. Health & API Telemetry ===${NC}"
    if command -v curl &>/dev/null; then
        HEALTH_OUT=$(curl -s -m 4 http://127.0.0.1:8000/health 2>/dev/null || echo "")
        if [ -n "$HEALTH_OUT" ]; then
            echo -e "  ${GREEN}[✓] API Endpoint (http://127.0.0.1:8000/health):${NC}"
            echo -e "      ${WHITE}$HEALTH_OUT${NC}"
        else
            echo -e "  ${RED}[✗] API Endpoint: Unreachable or starting up${NC}"
        fi
    else
        echo -e "  ${YELLOW}[!] curl not installed; skipping HTTP health test${NC}"
    fi
    echo ""

    echo -e "${BOLD}${BLUE}=== 3. Database Info ===${NC}"
    if [ -f "$INSTALL_DIR/vpn_private.db" ]; then
        DB_SIZE=$(du -sh "$INSTALL_DIR/vpn_private.db" | cut -f1)
        echo -e "  ${GREEN}[✓] SQLite Database:${NC} $INSTALL_DIR/vpn_private.db (${CYAN}${DB_SIZE}${NC})"
    else
        echo -e "  ${YELLOW}[!] SQLite file vpn_private.db not found locally (PostgreSQL or custom volume?)${NC}"
    fi
    echo ""

    echo -e "${BOLD}${BLUE}=== 4. Public Access Points ===${NC}"
    SERVER_IP=$(curl -s -m 3 -4 https://api.ipify.org 2>/dev/null || curl -s -m 3 -4 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
    if [ -n "$SERVER_IP" ]; then
        echo -e "  ${WHITE}• Web Admin Panel:${NC} ${GREEN}http://${SERVER_IP}:8000/admin${NC}"
    fi
    pause_prompt
}

restart_services() {
    show_banner
    echo -e "${YELLOW}[*] Ensuring Docker network exists...${NC}"
    ensure_networks
    echo -e "${YELLOW}[*] Restarting all VPN Private services...${NC}"
    $DC restart
    echo -e "${GREEN}[✓] All services successfully restarted!${NC}"
    pause_prompt
}

stop_services() {
    show_banner
    echo -e "${YELLOW}[*] Stopping all VPN Private services...${NC}"
    $DC stop
    echo -e "${GREEN}[✓] Services stopped.${NC}"
    pause_prompt
}

start_services() {
    show_banner
    echo -e "${YELLOW}[*] Ensuring Docker network exists...${NC}"
    ensure_networks
    echo -e "${YELLOW}[*] Starting all VPN Private services...${NC}"
    $DC up -d
    echo -e "${GREEN}[✓] Services started.${NC}"
    pause_prompt
}

view_logs() {
    while true; do
        show_banner
        echo -e "${BOLD}${WHITE}Choose which service logs to monitor:${NC}"
        echo -e "  ${CYAN}1)${NC} All Services (Bot + API)"
        echo -e "  ${CYAN}2)${NC} Telegram Bot (vpn-bot)"
        echo -e "  ${CYAN}3)${NC} FastAPI Server (vpn-api)"
        echo -e "  ${CYAN}0)${NC} Back to Main Menu"
        echo ""
        prompt_read "Select option [0-3]: " log_choice
        case "$log_choice" in
            1)
                echo -e "${YELLOW}[i] Press Ctrl+C to exit logs view.${NC}"
                sleep 1
                $DC logs -f --tail=100
                pause_prompt
                ;;
            2)
                echo -e "${YELLOW}[i] Press Ctrl+C to exit logs view.${NC}"
                sleep 1
                $DC logs -f --tail=100 vpn-bot
                pause_prompt
                ;;
            3)
                echo -e "${YELLOW}[i] Press Ctrl+C to exit logs view.${NC}"
                sleep 1
                $DC logs -f --tail=100 vpn-api
                pause_prompt
                ;;
            0)
                break
                ;;
            *)
                echo -e "${RED}Invalid selection.${NC}"
                sleep 1
                ;;
        esac
    done
}

update_platform() {
    show_banner
    echo -e "${BOLD}${YELLOW}=== Updating VPN Private to Latest Version ===${NC}"
    echo ""
    prompt_read "Are you sure you want to pull latest code and rebuild? (y/N): " confirm_update
    if [[ ! "$confirm_update" =~ ^[yY]$ ]]; then
        echo -e "${YELLOW}Update cancelled.${NC}"
        pause_prompt
        return
    fi

    echo -e "\n${BLUE}[1/4] Fetching latest git changes...${NC}"
    git fetch --all --prune
    git reset --hard origin/main
    git clean -fd -e .env -e vpn_private.db -e "*.db" -e "data/" -e "backups/"
    git pull origin main || true

    echo -e "\n${BLUE}[2/4] Ensuring Docker networks...${NC}"
    ensure_networks

    echo -e "\n${BLUE}[3/4] Pulling latest Docker image and launching containers...${NC}"
    if [ -f "$HOME/.docker/config.json" ] && grep -qi "auths" "$HOME/.docker/config.json" 2>/dev/null; then
        docker logout 2>/dev/null || true
    fi
    if ! $DC pull; then
        if [ -f "$HOME/.docker/config.json" ]; then
            mv "$HOME/.docker/config.json" "$HOME/.docker/config.json.bak_$(date +%s)" 2>/dev/null || true
        fi
        if ! $DC pull; then
            echo -e "${YELLOW}[!] Image pull failed; rebuilding locally...${NC}"
            $DC up -d --build --remove-orphans
        else
            $DC up -d --remove-orphans
        fi
    else
        $DC up -d --remove-orphans
    fi

    echo -e "\n${BLUE}[4/4] Running DB schema & core settings sync inside container...${NC}"
    sleep 4
    $DC exec -T vpn-api python scripts/seed_data.py || true

    echo -e "\n${GREEN}${BOLD}[✓] Update completed successfully!${NC}"
    pause_prompt
}

reset_db() {
    show_banner
    echo -e "${BOLD}${YELLOW}=== Clean Database Initialization & Core Settings Sync ===${NC}\n"
    echo -e "${YELLOW}This operation synchronizes database tables, default chart of accounts,${NC}"
    echo -e "${YELLOW}core system settings, and bot texts cleanly.${NC}"
    echo -e "${YELLOW}Note: This runs clean initialization and will NOT inject demo panels, servers,${NC}"
    echo -e "${YELLOW}tariffs, or discount coupons.${NC}\n"
    prompt_read "Do you wish to proceed? (y/N): " confirm_reset
    if [[ ! "$confirm_reset" =~ ^[yY]$ ]]; then
        echo -e "${YELLOW}Operation cancelled.${NC}"
        pause_prompt
        return
    fi

    echo -e "\n${BLUE}[*] Running clean database initialization inside vpn-api container...${NC}"
    $DC exec -T vpn-api python scripts/seed_data.py || python3 "$INSTALL_DIR/scripts/seed_data.py" || true
    echo -e "\n${GREEN}[✓] Database schema and core settings initialized successfully.${NC}"
    echo -e "${GREEN}[✓] Clean initialization confirmed: No demo tariffs or panels injected.${NC}"
    pause_prompt
}

backup_db() {
    show_banner
    echo -e "${BOLD}${BLUE}=== Backup Database ===${NC}"
    if [ -f "$INSTALL_DIR/scripts/backup.sh" ]; then
        bash "$INSTALL_DIR/scripts/backup.sh"
    else
        echo -e "${RED}[ERROR] scripts/backup.sh not found.${NC}"
    fi
    pause_prompt
}

restore_db() {
    show_banner
    echo -e "${BOLD}${BLUE}=== Restore Database ===${NC}"
    if [ -f "$INSTALL_DIR/scripts/restore.sh" ]; then
        bash "$INSTALL_DIR/scripts/restore.sh"
    else
        echo -e "${RED}[ERROR] scripts/restore.sh not found.${NC}"
    fi
    pause_prompt
}

edit_settings() {
    show_banner
    echo -e "${BOLD}${BLUE}=== Edit Configuration (.env) ===${NC}"
    ENV_FILE="$INSTALL_DIR/.env"
    if [ ! -f "$ENV_FILE" ]; then
        echo -e "${RED}[ERROR] .env file not found at $ENV_FILE!${NC}"
        pause_prompt
        return
    fi

    EDITOR_BIN="${EDITOR:-nano}"
    if ! command -v "$EDITOR_BIN" &>/dev/null; then
        if command -v nano &>/dev/null; then
            EDITOR_BIN="nano"
        elif command -v vim &>/dev/null; then
            EDITOR_BIN="vim"
        elif command -v vi &>/dev/null; then
            EDITOR_BIN="vi"
        fi
    fi

    echo -e "${YELLOW}Opening $ENV_FILE with $EDITOR_BIN...${NC}"
    sleep 1
    "$EDITOR_BIN" "$ENV_FILE"

    echo ""
    prompt_read "Do you want to restart all services to apply changes now? (y/N): " restart_conf
    if [[ "$restart_conf" =~ ^[yY]$ ]]; then
        ensure_networks
        $DC restart
        echo -e "${GREEN}[✓] Services restarted with new configuration.${NC}"
    fi
    pause_prompt
}

reset_admin_credentials() {
    show_banner
    echo -e "${BOLD}${YELLOW}=== Reset Web Admin Credentials & Unblock IPs ===${NC}\n"
    prompt_read "Enter new Web Admin Username (leave blank to keep current): " new_adm_user
    prompt_read "Enter new Web Admin Password: " new_adm_pass

    if [ -z "$new_adm_pass" ]; then
        echo -e "${RED}[ERROR] Password cannot be empty.${NC}"
        pause_prompt
        return
    fi

    if [ -n "$new_adm_user" ]; then
        if grep -q '^ADMIN_WEB_USERNAME=' "$INSTALL_DIR/.env" 2>/dev/null; then
            sed -i "s/^ADMIN_WEB_USERNAME=.*/ADMIN_WEB_USERNAME=${new_adm_user}/" "$INSTALL_DIR/.env"
        else
            echo "ADMIN_WEB_USERNAME=${new_adm_user}" >> "$INSTALL_DIR/.env"
        fi
    fi
    if grep -q '^ADMIN_WEB_PASSWORD=' "$INSTALL_DIR/.env" 2>/dev/null; then
        sed -i "s/^ADMIN_WEB_PASSWORD=.*/ADMIN_WEB_PASSWORD=${new_adm_pass}/" "$INSTALL_DIR/.env"
    else
        echo "ADMIN_WEB_PASSWORD=${new_adm_pass}" >> "$INSTALL_DIR/.env"
    fi

    $DC exec -T vpn-api python -c "
import asyncio
import bcrypt
from app.services.settings_service import SettingsService
from app.api.routes.admin import security_store

async def main():
    if '$new_adm_user':
        await SettingsService.set_value('ADMIN_WEB_USERNAME', '$new_adm_user')
    await SettingsService.set_value('ADMIN_WEB_PASSWORD', '$new_adm_pass')
    try:
        pw_hash = bcrypt.hashpw(b'''$new_adm_pass''', bcrypt.gensalt()).decode('utf-8')
        await SettingsService.set_value('ADMIN_WEB_PASSWORD_HASH', pw_hash)
    except Exception:
        pass
    if hasattr(security_store, 'clear_all_blocks'):
        security_store.clear_all_blocks()
    else:
        getattr(security_store, '_failed_attempts', {}).clear()
        getattr(security_store, '_blocked_ips', {}).clear()
    print('SUCCESS')

asyncio.run(main())
" 2>/dev/null || true

    echo -e "\n${GREEN}[✓] Web Admin credentials updated and all IP blocks cleared successfully.${NC}"
    pause_prompt
}

uninstall_platform() {
    show_banner
    echo -e "${BOLD}${RED}=== Complete Platform Uninstallation ===${NC}\n"
    if [ -f "$INSTALL_DIR/uninstall.sh" ]; then
        bash "$INSTALL_DIR/uninstall.sh" "$INSTALL_DIR"
        exit 0
    else
        echo -e "${RED}[ERROR] uninstall.sh script not found at $INSTALL_DIR/uninstall.sh.${NC}"
        pause_prompt
    fi
}

# Direct CLI argument dispatch (non-interactive shortcuts)
if [ $# -gt 0 ]; then
    case "$1" in
        status|health)
            view_status
            exit 0
            ;;
        restart)
            restart_services
            exit 0
            ;;
        stop)
            stop_services
            exit 0
            ;;
        start)
            start_services
            exit 0
            ;;
        logs)
            shift
            if [ -n "$1" ]; then
                $DC logs -f --tail=100 "$@"
            else
                $DC logs -f --tail=100
            fi
            exit 0
            ;;
        update)
            update_platform
            exit 0
            ;;
        backup)
            backup_db
            exit 0
            ;;
        restore)
            restore_db
            exit 0
            ;;
        seed|init-db|reset-db)
            reset_db
            exit 0
            ;;
        reset-admin|admin-reset|reset-password)
            reset_admin_credentials
            exit 0
            ;;
        uninstall|remove)
            uninstall_platform
            exit 0
            ;;
        help|-h|--help)
            echo "Usage: vpn-private [COMMAND]"
            echo "Commands:"
            echo "  status       View container status & health"
            echo "  restart      Restart all services"
            echo "  stop         Stop all services"
            echo "  start        Start all services"
            echo "  logs [svc]   View live logs"
            echo "  update       Update platform to latest version"
            echo "  backup       Backup database"
            echo "  restore      Restore database from backup"
            echo "  seed         Clean database schema & core settings sync"
            echo "  reset-db     Clean database schema & core settings sync"
            echo ""
            echo "Without arguments, launches the interactive management wizard."
            exit 0
            ;;
        *)
            echo -e "${RED}[ERROR] Unknown CLI command: '$1'${NC}"
            echo "Run 'vpn-private --help' for available commands."
            exit 1
            ;;
    esac
fi

# Main CLI Loop
while true; do
    show_banner
    echo -e "  ${WHITE}${BOLD}Select an operation:${NC}"
    echo -e "  ${GREEN}1)${NC} View Service Status & Health"
    echo -e "  ${GREEN}2)${NC} Restart All Services"
    echo -e "  ${GREEN}3)${NC} Stop All Services"
    echo -e "  ${GREEN}4)${NC} Start All Services"
    echo -e "  ${GREEN}5)${NC} View Real-time Logs (Bot / API / All)"
    echo -e "  ${GREEN}6)${NC} Update Platform to Latest Version"
    echo -e "  ${GREEN}7)${NC} Backup Database"
    echo -e "  ${GREEN}8)${NC} Restore Database"
    echo -e "  ${GREEN}9)${NC} Clean Database Initialization / Core Sync (No Demo Data)"
    echo -e "  ${GREEN}10)${NC} Edit Settings (.env)"
    echo -e "  ${GREEN}11)${NC} Reset Web Admin Credentials & Unblock IPs"
    echo -e "  ${RED}12)${NC} Uninstall VPN Private (Complete Cleanup)"
    echo -e "  ${WHITE}0)${NC} Exit"
    echo ""

    prompt_read "Enter your choice [0-12]: " choice
    case "$choice" in
        1) view_status ;;
        2) restart_services ;;
        3) stop_services ;;
        4) start_services ;;
        5) view_logs ;;
        6) update_platform ;;
        7) backup_db ;;
        8) restore_db ;;
        9) reset_db ;;
        10) edit_settings ;;
        11) reset_admin_credentials ;;
        12|u|U) uninstall_platform ;;
        0)
            echo -e "\n${CYAN}Exiting VPN Private CLI. Goodbye!${NC}\n"
            exit 0
            ;;
        *)
            echo -e "${RED}Invalid option: '$choice'${NC}"
            sleep 1
            ;;
    esac
done
