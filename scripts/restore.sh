#!/bin/bash
# =====================================================================
# Database Disaster Recovery & Restore Script for VPN Private
# =====================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/vpnprivate}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

BACKUP_FILE="${1:-}"

# If no file specified, list available backups in BACKUP_DIR
if [ -z "${BACKUP_FILE}" ]; then
    echo "================================================="
    echo "       VPN Private - Database Restore Wizard     "
    echo "================================================="
    if [ ! -d "${BACKUP_DIR}" ] || [ -z "$(ls -A "${BACKUP_DIR}" 2>/dev/null)" ]; then
        echo "No backups found in ${BACKUP_DIR}."
        read -r -p "Enter full path to backup file (.db.gz, .sql.gz, .db): " BACKUP_FILE < /dev/tty || true
    else
        echo "Available backups in ${BACKUP_DIR}:"
        backups=($(ls -t "${BACKUP_DIR}"/vpn_private_* 2>/dev/null || true))
        if [ ${#backups[@]} -eq 0 ]; then
            echo "No backups found."
            read -r -p "Enter full path to backup file: " BACKUP_FILE < /dev/tty || true
        else
            for i in "${!backups[@]}"; do
                size=$(du -sh "${backups[$i]}" 2>/dev/null | cut -f1 || echo "")
                echo "  $((i+1))) $(basename "${backups[$i]}") ($size)"
            done
            echo "  0) Cancel"
            read -r -p "Select backup number [1-${#backups[@]}]: " choice < /dev/tty || true
            if [ "$choice" = "0" ] || [ -z "$choice" ]; then
                echo "Restore cancelled."
                exit 0
            fi
            idx=$((choice-1))
            if [ $idx -ge 0 ] && [ $idx -lt ${#backups[@]} ]; then
                BACKUP_FILE="${backups[$idx]}"
            else
                echo "Invalid selection."
                exit 1
            fi
        fi
    fi
fi

if [ -z "${BACKUP_FILE}" ] || [ ! -f "${BACKUP_FILE}" ]; then
    echo "ERROR: Backup file '${BACKUP_FILE}' not found!" >&2
    exit 1
fi

echo "⚠️  WARNING: This will overwrite the current live database!"
echo "Selected backup: ${BACKUP_FILE}"
read -r -p "Are you sure you want to proceed with restore? (yes/no): " CONFIRM < /dev/tty || true
if [ "${CONFIRM}" != "yes" ]; then
    echo "Restore aborted."
    exit 0
fi

# Detect docker compose
DC="docker compose"
if ! docker compose version &>/dev/null; then
    if command -v docker-compose &>/dev/null; then
        DC="docker-compose"
    fi
fi

echo "[$(date)] Stopping services (vpn-api, vpn-bot)..."
$DC stop vpn-api vpn-bot || true

if [[ "${BACKUP_FILE}" == *.sql.gz || "${BACKUP_FILE}" == *.sql ]]; then
    echo "[$(date)] Restoring PostgreSQL database from ${BACKUP_FILE}..."
    if [[ "${BACKUP_FILE}" == *.gz ]]; then
        gunzip -c "${BACKUP_FILE}" | docker exec -i vpnprivate-postgres psql -U vpn_user -d vpn_private
    else
        cat "${BACKUP_FILE}" | docker exec -i vpnprivate-postgres psql -U vpn_user -d vpn_private
    fi
elif [[ "${BACKUP_FILE}" == *.db.gz || "${BACKUP_FILE}" == *.db || "${BACKUP_FILE}" == *.sqlite* ]]; then
    DB_TARGET="${SCRIPT_DIR}/vpn_private.db"
    echo "[$(date)] Restoring SQLite database to ${DB_TARGET}..."
    if [[ "${BACKUP_FILE}" == *.gz ]]; then
        gunzip -c "${BACKUP_FILE}" > "${DB_TARGET}"
    else
        cp -f "${BACKUP_FILE}" "${DB_TARGET}"
    fi
    chmod 644 "${DB_TARGET}"
else
    echo "ERROR: Unknown backup file format: ${BACKUP_FILE}" >&2
    $DC start vpn-api vpn-bot || true
    exit 1
fi

echo "[$(date)] Restarting services..."
$DC start vpn-api vpn-bot || $DC up -d

echo "[$(date)] Disaster recovery restore completed successfully!"
