#!/usr/bin/env bash
# =====================================================================
# VPN Private — Automated 1-Click Complete Uninstaller & Teardown
# =====================================================================
set -euo pipefail

# Terminal Color Codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
BOLD='\033[1m'
NC='\033[0m'

# Installation Path Auto-Discovery (supports custom installation paths)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
DISCOVERED_DIRS=()

add_candidate_dir() {
    local d="$1"
    d="$(echo "$d" | sed 's:/*$::')"
    [ -z "$d" ] && return
    [ "$d" = "/" ] && return
    for existing in "${DISCOVERED_DIRS[@]:-}"; do
        [ "$existing" = "$d" ] && return
    done
    if [ -d "$d" ]; then
        DISCOVERED_DIRS+=("$d")
    fi
}

# 1. Explicit CLI argument or environment variable
if [ -n "${1:-}" ]; then
    add_candidate_dir "$1"
fi
if [ -n "${VPN_INSTALL_DIR:-}" ]; then
    add_candidate_dir "$VPN_INSTALL_DIR"
fi

# 2. Current script directory (if uninstall.sh is inside the repo)
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
    add_candidate_dir "$SCRIPT_DIR"
fi

# 3. Recorded installation directory from /etc/vpn-private/install_dir
if [ -f "/etc/vpn-private/install_dir" ]; then
    SAVED_DIR="$(head -n 1 /etc/vpn-private/install_dir 2>/dev/null | tr -d '\r\n' || true)"
    [ -n "$SAVED_DIR" ] && add_candidate_dir "$SAVED_DIR"
fi

# 4. Symlink target of /usr/local/bin/vpn-private
if [ -L "/usr/local/bin/vpn-private" ] || [ -f "/usr/local/bin/vpn-private" ]; then
    CLI_TARGET="$(readlink -f /usr/local/bin/vpn-private 2>/dev/null || readlink /usr/local/bin/vpn-private 2>/dev/null || true)"
    if [ -n "$CLI_TARGET" ]; then
        CLI_PARENT="$(cd "$(dirname "$CLI_TARGET")/.." 2>/dev/null && pwd || true)"
        [ -n "$CLI_PARENT" ] && [ -f "$CLI_PARENT/docker-compose.yml" ] && add_candidate_dir "$CLI_PARENT"
    fi
fi

# 5. Inspect running/stopped Docker containers (com.docker.compose.project.working_dir & bind mounts)
if command -v docker &>/dev/null; then
    for cname in vpn-api vpn-bot; do
        C_WORKDIR="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$cname" 2>/dev/null || true)"
        [ -n "$C_WORKDIR" ] && add_candidate_dir "$C_WORKDIR"
        C_ENV_SRC="$(docker inspect --format '{{ range .Mounts }}{{ if eq .Destination "/app/.env" }}{{ .Source }}{{ end }}{{ end }}' "$cname" 2>/dev/null || true)"
        if [ -n "$C_ENV_SRC" ]; then
            add_candidate_dir "$(dirname "$C_ENV_SRC")"
        fi
    done
fi

# 6. Default installation path
if [ -d "/opt/vpn-private" ]; then
    add_candidate_dir "/opt/vpn-private"
fi

if [ "${#DISCOVERED_DIRS[@]}" -gt 0 ]; then
    DEFAULT_INSTALL_DIR="${DISCOVERED_DIRS[0]}"
else
    DEFAULT_INSTALL_DIR="${1:-${VPN_INSTALL_DIR:-/opt/vpn-private}}"
fi
INSTALL_DIR="$DEFAULT_INSTALL_DIR"
BACKUP_DIR="/var/backups/vpnprivate"

# TTY Device Detection (Support piped execution: bash <(curl -Ls ...))
TTY_DEV="/dev/tty"
if [ ! -e "$TTY_DEV" ] || [ ! -r "$TTY_DEV" ]; then
    TTY_DEV="/dev/stdin"
fi

prompt_input() {
    local prompt_msg="$1"
    local default_val="$2"
    local var_name="$3"
    local input_val=""

    if [ -n "$default_val" ]; then
        printf "%b" "${WHITE}${prompt_msg} [${YELLOW}${default_val}${WHITE}]: ${NC}" > "$TTY_DEV"
    else
        printf "%b" "${WHITE}${prompt_msg}: ${NC}" > "$TTY_DEV"
    fi

    read -r input_val < "$TTY_DEV" || true
    if [ -z "$input_val" ]; then
        input_val="$default_val"
    fi
    eval "$var_name=\"\$input_val\""
}

# 1. Root Privilege Verification
if [ "$(id -u)" != "0" ]; then
    echo -e "${RED}[ERROR] This uninstaller must be run with root privileges.${NC}" >&2
    echo -e "Please re-run with: ${YELLOW}sudo bash $0${NC} or as root." >&2
    exit 1
fi

clear 2>/dev/null || true
echo -e "${RED}${BOLD}"
cat << "EOF"
  ╔══════════════════════════════════════════════════════════════╗
  ║                 VPN PRIVATE — UNINSTALLER                    ║
  ║          Complete Clean Removal & Teardown Wizard            ║
  ╚══════════════════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

echo -e "${YELLOW}⚠️  WARNING: This operation will completely remove the VPN Private platform,${NC}"
echo -e "${YELLOW}   stop all running containers, remove CLI tools, and optionally wipe data.${NC}\n"

if [ "${#DISCOVERED_DIRS[@]}" -gt 0 ]; then
    echo -e "${CYAN}Detected VPN Private installation directory(s):${NC}"
    for d in "${DISCOVERED_DIRS[@]}"; do
        echo -e "  ${GREEN}•${NC} ${BOLD}${d}${NC}"
    done
    echo ""
fi

# Allow user to confirm or specify a custom installation directory
prompt_input "Installation directory to remove (press Enter to confirm or enter custom path)" "$INSTALL_DIR" CUSTOM_DIR_INPUT
CUSTOM_DIR_INPUT="$(echo "$CUSTOM_DIR_INPUT" | sed 's:/*$::')"
if [[ "$CUSTOM_DIR_INPUT" == ~* ]]; then
    CUSTOM_DIR_INPUT="${HOME}${CUSTOM_DIR_INPUT#\~}"
fi
if [ -n "$CUSTOM_DIR_INPUT" ] && [ "$CUSTOM_DIR_INPUT" != "/" ]; then
    INSTALL_DIR="$CUSTOM_DIR_INPUT"
    add_candidate_dir "$INSTALL_DIR"
fi
echo -e "${GREEN}[✓] Target directory for removal:${NC} ${BOLD}${CYAN}${INSTALL_DIR}${NC}\n"

# 2. First Confirmation
prompt_input "Are you sure you want to uninstall VPN Private? (y/N)" "N" CONFIRM_UNINSTALL
if [[ ! "$CONFIRM_UNINSTALL" =~ ^[yY]$ ]]; then
    echo -e "\n${GREEN}[✓] Uninstall cancelled. No changes were made.${NC}\n"
    exit 0
fi

# 3. Second Strict Confirmation
prompt_input "Type 'DELETE' to confirm complete uninstallation" "" STRICT_CONFIRM
if [ "$STRICT_CONFIRM" != "DELETE" ] && [ "$STRICT_CONFIRM" != "delete" ]; then
    echo -e "\n${GREEN}[✓] Confirmation mismatched. Uninstall aborted.${NC}\n"
    exit 0
fi

# 4. Database & Credentials Backup Option
echo ""
prompt_input "Do you want to save a backup of your database & .env before removal? (Y/n)" "Y" DO_BACKUP
FINAL_BACKUP_PATH=""
if [[ "$DO_BACKUP" =~ ^[yY]$ ]] || [ -z "$DO_BACKUP" ]; then
    if [ -d "$INSTALL_DIR" ]; then
        mkdir -p "$BACKUP_DIR"
        TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
        FINAL_BACKUP_PATH="${BACKUP_DIR}/vpn_private_final_backup_${TIMESTAMP}.tar.gz"
        echo -e "\n${BLUE}[*] Archiving database and .env configuration to ${FINAL_BACKUP_PATH}...${NC}"

        tar -czf "$FINAL_BACKUP_PATH" \
            -C "$INSTALL_DIR" \
            $(test -f "$INSTALL_DIR/.env" && echo ".env") \
            $(test -f "$INSTALL_DIR/vpn_private.db" && echo "vpn_private.db") \
            2>/dev/null || true

        if [ -s "$FINAL_BACKUP_PATH" ]; then
            echo -e "${GREEN}[✓] Safety backup successfully preserved at: ${FINAL_BACKUP_PATH}${NC}"
        else
            echo -e "${YELLOW}[!] Backup archive empty or files not found. Continuing removal...${NC}"
        fi
    fi
fi

# 5. Stop and Remove Docker Containers
echo -e "\n${BLUE}[1/5] Stopping and removing Docker containers and networks...${NC}"
for target_d in "${DISCOVERED_DIRS[@]:-$INSTALL_DIR}"; do
    if [ -d "$target_d" ] && [ -f "$target_d/docker-compose.yml" ]; then
        cd "$target_d"
        if command -v docker &>/dev/null; then
            if docker compose version &>/dev/null; then
                docker compose down -v --remove-orphans 2>/dev/null || true
            elif command -v docker-compose &>/dev/null; then
                docker-compose down -v --remove-orphans 2>/dev/null || true
            fi
        fi
    fi
done
cd / 2>/dev/null || true

# Fallback: force remove any remaining vpn- containers
if command -v docker &>/dev/null; then
    docker rm -f vpn-api vpn-bot 2>/dev/null || true
fi
echo -e "${GREEN}[✓] Docker containers stopped and removed.${NC}"

# 6. Remove Docker Images (Optional cleanup)
echo -e "\n${BLUE}[2/5] Cleaning up Docker images...${NC}"
if command -v docker &>/dev/null; then
    docker rmi -f adprivate/vpn-private:latest 2>/dev/null || true
    docker rmi -f vpn-private:latest 2>/dev/null || true
fi
echo -e "${GREEN}[✓] Docker images cleaned.${NC}"

# 7. Remove Global CLI Management Tool
echo -e "\n${BLUE}[3/5] Removing global CLI symlink (/usr/local/bin/vpn-private)...${NC}"
rm -f /usr/local/bin/vpn-private
rm -rf /etc/vpn-private 2>/dev/null || true
echo -e "${GREEN}[✓] Global CLI command removed.${NC}"

# 8. Clean Up Legacy Systemd Services (if any)
echo -e "\n${BLUE}[4/5] Checking and cleaning legacy systemd services...${NC}"
if command -v systemctl &>/dev/null; then
    systemctl stop vpn-bot.service vpn-api.service 2>/dev/null || true
    systemctl disable vpn-bot.service vpn-api.service 2>/dev/null || true
    rm -f /etc/systemd/system/vpn-bot.service /etc/systemd/system/vpn-api.service
    systemctl daemon-reload 2>/dev/null || true
fi
echo -e "${GREEN}[✓] Services disabled.${NC}"

# 9. Remove Installation Directory (including any custom installation directory)
echo -e "\n${BLUE}[5/5] Removing installation directory (${INSTALL_DIR})...${NC}"
cd / 2>/dev/null || true
if [ -n "$INSTALL_DIR" ] && [ "$INSTALL_DIR" != "/" ] && [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    echo -e "${GREEN}[✓] Removed directory: ${INSTALL_DIR}${NC}"
fi
for extra_d in "${DISCOVERED_DIRS[@]:-}"; do
    if [ -n "$extra_d" ] && [ "$extra_d" != "/" ] && [ "$extra_d" != "$INSTALL_DIR" ] && [ -d "$extra_d" ]; then
        rm -rf "$extra_d"
        echo -e "${GREEN}[✓] Removed additional detected directory: ${extra_d}${NC}"
    fi
done
echo -e "${GREEN}[✓] Installation files completely wiped.${NC}"

# 10. Completion Banner
echo -e "\n${GREEN}${BOLD}"
cat << "EOF"
  ╔══════════════════════════════════════════════════════════════╗
  ║           🎉 UNINSTALLATION COMPLETED SUCCESSFULLY!          ║
  ║      VPN Private has been completely removed from server.    ║
  ╚══════════════════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

if [ -n "$FINAL_BACKUP_PATH" ] && [ -s "$FINAL_BACKUP_PATH" ]; then
    echo -e "${CYAN}ℹ️  Your database & config backup was saved at:${NC}"
    echo -e "   ${YELLOW}${FINAL_BACKUP_PATH}${NC}\n"
fi

echo -e "${WHITE}To re-install in the future at any time, run:${NC}"
echo -e "   ${GREEN}bash <(curl -Ls https://raw.githubusercontent.com/Adler-tak/vpn-private/main/install.sh)${NC}\n"
