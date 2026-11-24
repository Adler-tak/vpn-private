#!/bin/bash
# =====================================================================
# Automated Database Backup Script for VPN Private (SQLite & Postgres)
# =====================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/vpnprivate}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RETENTION_DAYS=30

mkdir -p "${BACKUP_DIR}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

echo "[$(date)] Starting backup of VPN Private database..."

if command -v docker &>/dev/null && docker ps --format '{{.Names}}' 2>/dev/null | grep -q 'vpnprivate-postgres'; then
    BACKUP_FILE="${BACKUP_DIR}/vpn_private_${TIMESTAMP}.sql.gz"
    echo "[$(date)] Backing up PostgreSQL container database..."
    docker exec -t vpnprivate-postgres pg_dump -U vpn_user -d vpn_private | gzip > "${BACKUP_FILE}"
elif [ -f "${SCRIPT_DIR}/vpn_private.db" ]; then
    BACKUP_FILE="${BACKUP_DIR}/vpn_private_${TIMESTAMP}.db.gz"
    echo "[$(date)] Backing up SQLite database (${SCRIPT_DIR}/vpn_private.db)..."
    if command -v sqlite3 &>/dev/null; then
        sqlite3 "${SCRIPT_DIR}/vpn_private.db" ".backup '${BACKUP_DIR}/temp_${TIMESTAMP}.db'"
        gzip -c "${BACKUP_DIR}/temp_${TIMESTAMP}.db" > "${BACKUP_FILE}"
        rm -f "${BACKUP_DIR}/temp_${TIMESTAMP}.db"
    else
        gzip -c "${SCRIPT_DIR}/vpn_private.db" > "${BACKUP_FILE}"
    fi
elif [ -f "/opt/vpn-private/vpn_private.db" ]; then
    BACKUP_FILE="${BACKUP_DIR}/vpn_private_${TIMESTAMP}.db.gz"
    echo "[$(date)] Backing up SQLite database (/opt/vpn-private/vpn_private.db)..."
    gzip -c "/opt/vpn-private/vpn_private.db" > "${BACKUP_FILE}"
else
    echo "[$(date)] ERROR: Neither PostgreSQL container nor SQLite vpn_private.db found!" >&2
    exit 1
fi

# Verify backup file size
if [ -s "${BACKUP_FILE}" ]; then
    echo "[$(date)] Backup successfully generated: ${BACKUP_FILE} ($(du -sh "${BACKUP_FILE}" | cut -f1))"
else
    echo "[$(date)] ERROR: Backup file is empty!" >&2
    exit 1
fi

# Apply retention policy: remove backups older than 30 days
echo "[$(date)] Purging backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -name "vpn_private_*" -type f -mtime +${RETENTION_DAYS} -delete 2>/dev/null || true

echo "[$(date)] Backup routine finished successfully."
