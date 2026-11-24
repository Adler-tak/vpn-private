#!/bin/bash
set -e

echo "[VPN Private Entrypoint] Checking environment..."

# Wait for PostgreSQL using native Python socket check
if [ -n "$POSTGRES_HOST" ]; then
  echo "[VPN Private Entrypoint] Waiting for PostgreSQL at $POSTGRES_HOST:5432..."
  python - <<END
import socket
import time
import sys

host = "$POSTGRES_HOST"
port = 5432
for i in range(30):
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"[VPN Private Entrypoint] PostgreSQL is ready on {host}:{port}")
            sys.exit(0)
    except Exception:
        time.sleep(1)
print(f"[VPN Private Entrypoint] ERROR: Timeout waiting for {host}:{port}")
sys.exit(1)
END
fi

# Execute passed command (e.g. bot, api, worker, beat)
exec "$@"
