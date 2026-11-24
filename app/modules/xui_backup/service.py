"""
X-UI Database Automatic Backup and Restore Service.
Handles safe SQLite WAL-mode backups, timestamped archiving,
pruning (keeping latest 50), Docker socket fallback extraction/restore,
and container restart on restore.
"""

import io
import os
import json
import shutil
import socket
import logging
import sqlite3
import tarfile
import subprocess
import urllib.parse
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple
from http.client import HTTPResponse
import requests
import urllib3
from app.integrations.xui.client import normalize_panel_base_url, generate_totp_code

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# Configurable paths with standard 3X-UI defaults
XUI_DB_PATH = os.getenv("XUI_DB_PATH", "/etc/x-ui/x-ui.db")
XUI_BACKUP_DIR = os.getenv("XUI_BACKUP_DIR", "/app/data/backups/xui")
DOCKER_CONTAINER = os.getenv("XUI_CONTAINER_NAME", "3x")


def get_active_xui_panels() -> List[Dict[str, Any]]:
    """
    Loads active 3X-UI panel records from the VPN Private database (SQLite) and decrypts credentials.
    Works synchronously from any thread (PIA/Windscribe worker threads or FastAPI endpoints).
    """
    from app.core.security import decrypt_credential
    panels: List[Dict[str, Any]] = []
    candidate_dbs = [
        "/app/vpn_private.db",
        "/opt/vpn-private/vpn_private.db",
        "./vpn_private.db",
        "vpn_private.db",
    ]
    for db_file in candidate_dbs:
        if os.path.isfile(db_file):
            try:
                conn = sqlite3.connect(db_file, timeout=10.0)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, name, base_url, username, password_encrypted, api_token_encrypted, login_secret_encrypted FROM panels WHERE is_active = 1"
                ).fetchall()
                if not rows:
                    rows = conn.execute(
                        "SELECT id, name, base_url, username, password_encrypted, api_token_encrypted, login_secret_encrypted FROM panels"
                    ).fetchall()
                conn.close()
                for r in rows:
                    d = dict(r)
                    pwd_enc = d.get("password_encrypted") or ""
                    tok_enc = d.get("api_token_encrypted") or ""
                    sec_enc = d.get("login_secret_encrypted") or ""
                    panels.append({
                        "id": d.get("id"),
                        "name": d.get("name"),
                        "base_url": d.get("base_url"),
                        "username": d.get("username"),
                        "password": decrypt_credential(pwd_enc) if pwd_enc else "",
                        "api_token": decrypt_credential(tok_enc) if tok_enc else "",
                        "login_secret": decrypt_credential(sec_enc) if sec_enc else "",
                    })
                if panels:
                    return panels
            except Exception as e:
                logger.debug(f"Could not read panels from {db_file}: {e}")
    return panels


def create_authenticated_xui_session(panel: Dict[str, Any]) -> Tuple[Optional[requests.Session], str]:
    """
    Creates an authenticated requests.Session logged into the specified 3X-UI panel API.
    Returns (session, normalized_base_url) or (None, base_url) on failure.
    """
    base_url = normalize_panel_base_url(panel.get("base_url") or "")
    if not base_url:
        return None, ""

    parsed_u = urllib.parse.urlparse(base_url)
    origin = f"{parsed_u.scheme}://{parsed_u.netloc}"

    sess = requests.Session()
    sess.verify = False
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Origin": origin,
        "Referer": base_url + "/",
        "X-Requested-With": "XMLHttpRequest",
    })

    try:
        get_resp = sess.get(base_url + "/", timeout=10)
        if get_resp.status_code == 200:
            import re
            m = re.search(r'name=["\']csrf-token["\'] content=["\']([^"\']+)["\']', get_resp.text)
            if m:
                sess.headers["X-CSRF-Token"] = m.group(1)
    except Exception:
        pass

    api_token = (panel.get("api_token") or "").strip()
    if api_token:
        sess.headers["Authorization"] = f"Bearer {api_token}"
        try:
            r = sess.get(f"{base_url}/panel/api/server/status", timeout=10)
            if r.status_code == 200 and r.json().get("success"):
                return sess, base_url
        except Exception:
            pass

    payload: Dict[str, Any] = {
        "username": panel.get("username") or "",
        "password": panel.get("password") or "",
    }
    login_secret = (panel.get("login_secret") or "").strip()
    if login_secret:
        otp_val = generate_totp_code(login_secret)
        payload["loginSecret"] = otp_val or login_secret
        if otp_val:
            payload["twoFactorCode"] = otp_val
            payload["code"] = otp_val

    try:
        r = sess.post(f"{base_url}/login", json=payload, timeout=12)
        if r.status_code != 200 or "text/html" in r.headers.get("content-type", ""):
            r = sess.post(f"{base_url}/login", data=payload, timeout=12)
        if r.status_code == 200 and r.json().get("success"):
            return sess, base_url
        logger.warning(f"3X-UI API login failed for {base_url}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"3X-UI API connection error for {base_url}: {e}")
    return None, base_url


def fetch_xray_config_via_api() -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Fetches live xrayTemplateConfig (xraySetting) via 3X-UI HTTP API (`/panel/api/xray/`).
    Returns (xray_setting_dict, panel_dict) or (None, None).
    """
    for panel in get_active_xui_panels():
        sess, base_url = create_authenticated_xui_session(panel)
        if not sess:
            continue
        try:
            resp = sess.post(f"{base_url}/panel/api/xray/", timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("obj"):
                    obj_raw = data["obj"]
                    obj = json.loads(obj_raw) if isinstance(obj_raw, str) else obj_raw
                    if isinstance(obj, dict):
                        xray_setting = obj.get("xraySetting") or obj
                        if isinstance(xray_setting, str):
                            xray_setting = json.loads(xray_setting)
                        if isinstance(xray_setting, dict) and "outbounds" in xray_setting:
                            return xray_setting, panel
        except Exception as e:
            logger.warning(f"Failed fetching xray config via API from {panel.get('name')}: {e}")
        finally:
            sess.close()
    return None, None


def update_xray_config_via_api(cfg: Dict[str, Any], target_panel: Optional[Dict[str, Any]] = None) -> Tuple[bool, bool]:
    """
    Saves updated xrayTemplateConfig (xraySetting) and restarts Xray service across active 3X-UI panel(s) via API.
    Returns (updated_ok, restarted_ok).
    """
    panels = [target_panel] if target_panel else get_active_xui_panels()
    if not panels:
        return False, False

    any_updated = False
    any_restarted = False
    cfg_str = json.dumps(cfg, ensure_ascii=False)

    for panel in panels:
        if not panel:
            continue
        sess, base_url = create_authenticated_xui_session(panel)
        if not sess:
            continue
        try:
            r_upd = sess.post(
                f"{base_url}/panel/api/xray/update",
                data={"xraySetting": cfg_str},
                timeout=20,
            )
            if r_upd.status_code == 200 and r_upd.json().get("success"):
                any_updated = True
                logger.info(f"Updated Xray config via 3X-UI API on panel {panel.get('name')}.")
                r_rst = sess.post(f"{base_url}/panel/api/server/restartXrayService", timeout=20)
                if r_rst.status_code == 200 and r_rst.json().get("success"):
                    any_restarted = True
                    logger.info(f"Restarted Xray service via 3X-UI API on panel {panel.get('name')}.")
        except Exception as e:
            logger.warning(f"Failed updating Xray config via API on {panel.get('name')}: {e}")
        finally:
            sess.close()

    return any_updated, any_restarted


def download_db_via_xui_api(target_path: Path) -> bool:
    """
    Downloads the SQLite database directly from the connected 3X-UI panel API (`/panel/api/server/getDb`).
    """
    for panel in get_active_xui_panels():
        sess, base_url = create_authenticated_xui_session(panel)
        if not sess:
            continue
        try:
            for ep in ["/panel/api/server/getDb", "/server/getDb"]:
                resp = sess.get(f"{base_url}{ep}", timeout=25)
                if resp.status_code == 200 and resp.content.startswith(b"SQLite format 3"):
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(target_path, "wb") as f_out:
                        f_out.write(resp.content)
                    logger.info(f"Downloaded 3X-UI database ({len(resp.content)} bytes) via API from {panel.get('name')}.")
                    return True
        except Exception as e:
            logger.warning(f"API database download failed on {panel.get('name')}: {e}")
        finally:
            sess.close()
    return False


def restore_db_via_xui_api(source_path: Path) -> bool:
    """
    Uploads and restores a SQLite backup file via 3X-UI panel API (`/panel/api/server/importDB`).
    """
    for panel in get_active_xui_panels():
        sess, base_url = create_authenticated_xui_session(panel)
        if not sess:
            continue
        try:
            for ep in ["/panel/api/server/importDB", "/server/importDB"]:
                with open(source_path, "rb") as f_in:
                    files = {"db": (source_path.name, f_in, "application/octet-stream")}
                    resp = sess.post(f"{base_url}{ep}", files=files, timeout=30)
                if resp.status_code == 200:
                    try:
                        if resp.json().get("success"):
                            logger.info(f"Restored 3X-UI database via API on {panel.get('name')}.")
                            return True
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"API database restore failed on {panel.get('name')}: {e}")
        finally:
            sess.close()
    return False


def find_active_xui_container() -> Optional[str]:
    """
    Dynamically discovers if 3X-UI is running in a Docker container.
    Scans running containers via /var/run/docker.sock for any container named
    '3x', 'x-ui', '3x-ui', 'xui' or images containing '3x-ui', 'x-ui', 'mhsanaei'.
    Returns container name (e.g. '3x') if in Docker, or None if running natively on Host OS.
    """
    configured = globals().get("DOCKER_CONTAINER") or os.getenv("XUI_CONTAINER_NAME")
    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return configured if configured else None

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(sock_path)
        s.sendall(b"GET /containers/json HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        resp = HTTPResponse(s)
        resp.begin()
        if resp.status == 200:
            body = resp.read().decode()
            s.close()
            containers = json.loads(body)

            # 1. Exact match for configured container name
            if configured:
                for c in containers:
                    for name in c.get("Names", []):
                        if name.lstrip("/") == configured:
                            return configured

            # 2. Known standard container names
            for c in containers:
                for name in c.get("Names", []):
                    clean_name = name.lstrip("/").lower()
                    if clean_name in ("3x", "x-ui", "3x-ui", "xui") or "3x-ui" in clean_name or "x-ui" in clean_name:
                        return name.lstrip("/")

            # 3. Known standard images (mhsanaei/3x-ui, etc.)
            for c in containers:
                img = c.get("Image", "").lower()
                if "3x-ui" in img or "x-ui" in img or "mhsanaei" in img:
                    names = c.get("Names", [])
                    if names:
                        return names[0].lstrip("/")
        else:
            s.close()
    except Exception as exc:
        logger.debug(f"Could not inspect running containers from Docker socket: {exc}")

    return None


def get_backup_dir() -> Path:
    """
    Returns the persistent directory for X-UI backups.
    Ensures directory exists and is writable.
    """
    configured = globals().get("XUI_BACKUP_DIR") or os.getenv("XUI_BACKUP_DIR")
    if configured:
        p = Path(configured)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception as e:
            logger.warning(f"Could not create configured backup dir {p}: {e}")

    candidates = [
        Path("/app/data/backups/xui"),
        Path("/opt/vpn-private/data/backups/xui"),
        Path("./data/backups/xui"),
        Path("/opt/vpn-private/backups/xui"),
    ]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except Exception:
            continue

    fallback = Path("./data/backups/xui")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def resolve_xui_db_path() -> Optional[Path]:
    """
    Dynamically finds the 3X-UI SQLite database file on local filesystem.
    Works seamlessly whether 3X-UI is running in a Docker container OR natively on the Host OS.
    Checks configured XUI_DB_PATH, standard mount points, and candidate filenames.
    """
    mod_path = globals().get("XUI_DB_PATH") or os.getenv("XUI_DB_PATH")
    if mod_path:
        p = Path(mod_path)
        if p.is_file():
            return p
        if p.is_dir():
            for name in ("x-ui.db", "3x-ui.db", "xui.db"):
                candidate = p / name
                if candidate.is_file():
                    return candidate

    potential_files = [
        # 1. Native Host OS standard Sanaei 3X-UI install
        Path("/etc/x-ui/x-ui.db"),
        # 2. Native Host OS Alireza / custom installations
        Path("/usr/local/x-ui/bin/x-ui.db"),
        Path("/usr/local/x-ui/x-ui.db"),
        # 3. Other common paths & variations
        Path("/etc/x-ui/3x-ui.db"),
        Path("/etc/x-ui/xui.db"),
        Path("/opt/3x-ui/db/x-ui.db"),
        Path("/app/data/x-ui/x-ui.db"),
        Path("/app/data/x-ui.db"),
        Path("./data/x-ui/x-ui.db"),
        Path("./data/x-ui.db"),
    ]
    for pf in potential_files:
        if pf.is_file():
            return pf

    potential_dirs = [
        Path("/etc/x-ui"),
        Path("/usr/local/x-ui/bin"),
        Path("/usr/local/x-ui"),
        Path("/opt/3x-ui/db"),
        Path("/app/data/x-ui"),
        Path("./data/x-ui"),
    ]
    for pd in potential_dirs:
        if pd.is_dir():
            for name in ("x-ui.db", "3x-ui.db", "xui.db"):
                candidate = pd / name
                if candidate.is_file():
                    return candidate
            try:
                for candidate in pd.glob("*.db"):
                    if candidate.is_file() and ("x-ui" in candidate.name or "3x" in candidate.name):
                        return candidate
            except Exception:
                pass

    return None


def extract_db_from_docker_socket(target_path: Path, container_name: Optional[str] = None) -> bool:
    """
    Extracts x-ui.db directly from the running 3x container using Docker Engine API
    over /var/run/docker.sock when local volume mounts are unavailable.
    """
    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return False

    c_name = container_name or find_active_xui_container() or globals().get("DOCKER_CONTAINER", "3x")
    candidate_paths = [
        "/etc/x-ui/x-ui.db",
        "/usr/local/x-ui/bin/x-ui.db",
        "/etc/x-ui/3x-ui.db",
    ]

    for remote_path in candidate_paths:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(15)
            s.connect(sock_path)

            req = (
                f"GET /containers/{c_name}/archive?path={remote_path} HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Connection: close\r\n\r\n"
            )
            s.sendall(req.encode())

            resp = HTTPResponse(s)
            resp.begin()

            if resp.status != 200:
                s.close()
                continue

            body = resp.read()
            s.close()

            if not body:
                continue

            with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tar:
                for member in tar.getmembers():
                    if member.name.endswith(".db") or "x-ui" in member.name:
                        extracted = tar.extractfile(member)
                        if extracted:
                            target_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(target_path, "wb") as f_out:
                                shutil.copyfileobj(extracted, f_out)
                            logger.info(
                                f"Successfully extracted {remote_path} from container {c_name} via Docker socket."
                            )
                            return True
        except Exception as exc:
            logger.warning(f"Docker socket extraction failed for {remote_path}: {exc}")

    return False


def restore_db_to_docker_socket(source_path: Path, container_name: Optional[str] = None) -> bool:
    """
    Sends a tar archive containing the backup database to the container via
    Docker Engine API PUT /containers/{id}/archive?path=/etc/x-ui.
    """
    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return False

    c_name = container_name or find_active_xui_container()
    if not c_name:
        # Host mode does not need socket restore since local filesystem overwrite already updated the file
        return False

    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            ti = tarfile.TarInfo(name="x-ui.db")
            ti.size = source_path.stat().st_size
            ti.mode = 0o600
            with open(source_path, "rb") as f_in:
                tar.addfile(ti, f_in)

        tar_bytes = buf.getvalue()

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(20)
        s.connect(sock_path)

        headers = (
            f"PUT /containers/{c_name}/archive?path=/etc/x-ui HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Content-Length: {len(tar_bytes)}\r\n"
            f"Content-Type: application/x-tar\r\n"
            f"Connection: close\r\n\r\n"
        )
        s.sendall(headers.encode() + tar_bytes)

        resp = HTTPResponse(s)
        resp.begin()
        success = (resp.status == 200)
        s.close()
        if success:
            logger.info(f"Successfully uploaded restored database to container {c_name} via Docker socket.")
            return True
        else:
            logger.warning(f"Docker socket PUT archive failed with status: {resp.status} {resp.reason}")
    except Exception as exc:
        logger.warning(f"Failed to restore DB to docker socket: {exc}")

    return False


def restart_3x_service() -> bool:
    """
    Restarts 3X-UI whether it runs in a Docker container OR natively on the Host OS.
    1. If a Docker container is detected, restarts the container via Docker Engine API.
    2. If running natively on Host OS, executes systemctl restart x-ui on host.
    """
    container_name = find_active_xui_container()

    # Mode 1: Docker Container Mode
    if container_name:
        logger.info(f"Restarting 3X-UI Docker container: '{container_name}'...")
        if os.path.exists("/var/run/docker.sock"):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(15)
                s.connect("/var/run/docker.sock")
                req = f"POST /containers/{container_name}/restart HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                s.sendall(req.encode())
                resp = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                s.close()
                if b"204 No Content" in resp or b"200 OK" in resp:
                    logger.info(f"Successfully restarted container '{container_name}' via unix socket.")
                    return True
            except Exception as e:
                logger.warning(f"Socket restart failed for '{container_name}': {e}")

        try:
            subprocess.run(["docker", "restart", container_name], check=True, timeout=25)
            logger.info(f"Successfully restarted container '{container_name}' via docker CLI.")
            return True
        except Exception as e:
            logger.error(f"Failed to restart container '{container_name}' via CLI: {e}")

    # Mode 2: Native Host OS Mode (systemctl / host runner)
    logger.info("3X-UI is running natively on Host OS. Executing host service restart...")
    sock_path = "/var/run/docker.sock"
    if os.path.exists(sock_path):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(15)
            s.connect(sock_path)
            payload = {
                "Image": "adprivate/vpn-private:latest",
                "Cmd": [
                    "chroot", "/host", "sh", "-c",
                    "systemctl restart x-ui 2>/dev/null || systemctl restart 3x-ui 2>/dev/null || /usr/local/x-ui/x-ui restart 2>/dev/null || service x-ui restart 2>/dev/null || true"
                ],
                "HostConfig": {
                    "Privileged": True,
                    "PidMode": "host",
                    "Binds": ["/:/host:ro"]
                }
            }
            b_bytes = json.dumps(payload).encode()
            req = (
                f"POST /containers/create HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(b_bytes)}\r\n\r\n"
            ).encode() + b_bytes
            s.sendall(req)
            resp = HTTPResponse(s)
            resp.begin()
            create_status = resp.status
            create_body = resp.read().decode()
            s.close()

            if create_status == 201:
                cid = json.loads(create_body)["Id"]

                s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s2.settimeout(15)
                s2.connect(sock_path)
                s2.sendall(f"POST /containers/{cid}/start HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
                r2 = HTTPResponse(s2)
                r2.begin()
                r2.read()
                s2.close()

                s3 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s3.settimeout(15)
                s3.connect(sock_path)
                s3.sendall(f"POST /containers/{cid}/wait HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
                r3 = HTTPResponse(s3)
                r3.begin()
                r3.read()
                s3.close()

                s4 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s4.settimeout(10)
                s4.connect(sock_path)
                s4.sendall(f"DELETE /containers/{cid}?force=true HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
                r4 = HTTPResponse(s4)
                r4.begin()
                r4.read()
                s4.close()

                logger.info("Successfully restarted host 3X-UI service via Docker host executor.")
                return True
        except Exception as e:
            logger.warning(f"Host restart via Docker socket failed: {e}")

    for cmd in [
        ["systemctl", "restart", "x-ui"],
        ["systemctl", "restart", "3x-ui"],
        ["/usr/local/x-ui/x-ui", "restart"],
        ["service", "x-ui", "restart"],
    ]:
        try:
            res = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if res.returncode == 0:
                logger.info(f"Successfully restarted host 3X-UI via {' '.join(cmd)}")
                return True
        except Exception:
            continue

    logger.warning("Could not restart 3X-UI: neither container nor host service restart succeeded.")
    return False


def restart_3x_container() -> bool:
    """Backwards-compatible wrapper for restart_3x_service."""
    return restart_3x_service()


def prune_xui_backups(max_keep: int = 50) -> int:
    """
    Keeps the latest max_keep backups and removes older backups.
    Returns the number of pruned files.
    """
    backup_dir = get_backup_dir()
    if not backup_dir.exists():
        return 0

    backups = []
    for f in backup_dir.glob("x-ui_*.db"):
        if f.is_file():
            try:
                backups.append((f.stat().st_mtime, f))
            except Exception:
                pass

    # Sort descending (newest first)
    backups.sort(key=lambda x: x[0], reverse=True)

    pruned = 0
    if len(backups) > max_keep:
        for _, file_path in backups[max_keep:]:
            try:
                file_path.unlink()
                pruned += 1
                logger.info(f"Pruned old X-UI backup: {file_path.name}")
            except Exception as e:
                logger.warning(f"Failed to delete old backup {file_path}: {e}")

    return pruned


def create_xui_backup(action_name: str = "auto") -> Optional[str]:
    """
    Creates a timestamped copy of 3X-UI database named x-ui_YYYYMMDD_HHMMSS_{action_name}.db.
    Safely handles SQLite WAL mode and prunes to keep the latest 50 backups.
    Returns the backup filename or None on error.
    """
    backup_dir = get_backup_dir()
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create backup directory {backup_dir}: {e}")
        return None

    safe_action = "".join(c for c in action_name if c.isalnum() or c in ("-", "_")) or "auto"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"x-ui_{timestamp}_{safe_action}.db"
    target_path = backup_dir / backup_filename

    db_path = resolve_xui_db_path()
    backup_success = False

    # Attempt 0: Direct download via 3X-UI REST API (/panel/api/server/getDb)
    backup_success = download_db_via_xui_api(target_path)

    # Attempt 1: Local SQLite online backup (WAL safe)
    if not backup_success and db_path and db_path.exists():
        try:
            src_conn = sqlite3.connect(str(db_path), timeout=30.0)
            try:
                try:
                    src_conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                except Exception as cp_err:
                    logger.warning(f"WAL checkpoint warning: {cp_err}")

                dst_conn = sqlite3.connect(str(target_path))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

            backup_success = True
            logger.info(f"Created X-UI backup from local file {db_path} -> {target_path}")
        except Exception as e:
            logger.warning(f"Local SQLite backup failed ({e}), falling back to Docker socket...")

    # Attempt 2: Direct extraction from container via Docker socket
    if not backup_success:
        backup_success = extract_db_from_docker_socket(target_path)

    if not backup_success or not target_path.exists():
        logger.error("Failed to create X-UI backup: database not accessible via filesystem or Docker socket.")
        if target_path.exists():
            try:
                target_path.unlink()
            except Exception:
                pass
        return None

    try:
        # Integrity verification
        verif_conn = sqlite3.connect(str(target_path))
        verif_conn.execute("SELECT 1;").fetchone()
        verif_conn.close()

        logger.info(f"Successfully verified X-UI backup: {backup_filename} at {target_path}")

        # Prune to latest 50
        prune_xui_backups(max_keep=50)

        return backup_filename
    except Exception as e:
        logger.error(f"Failed to verify X-UI backup integrity: {e}", exc_info=True)
        if target_path.exists():
            try:
                target_path.unlink()
            except Exception:
                pass
        return None


def list_xui_backups() -> List[Dict[str, Any]]:
    """
    Returns list of existing backup files sorted by creation date descending.
    Each item contains name, size_bytes, size_mb, created_at.
    """
    search_dirs = [get_backup_dir()]
    # Also check legacy directories if distinct
    for leg in [Path("/opt/vpn-private/backups/xui"), Path("/app/data/backups/xui"), Path("./data/backups/xui")]:
        if leg not in search_dirs and leg.exists():
            search_dirs.append(leg)

    seen_names = set()
    results = []

    for b_dir in search_dirs:
        if not b_dir.exists():
            continue
        for f in b_dir.glob("x-ui_*.db"):
            if f.is_file() and f.name not in seen_names:
                try:
                    st = f.stat()
                    size_bytes = st.st_size
                    size_mb = round(size_bytes / (1024 * 1024), 2)
                    created_at = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                    results.append({
                        "name": f.name,
                        "size_bytes": size_bytes,
                        "size_mb": size_mb,
                        "created_at": created_at,
                        "mtime": st.st_mtime,
                    })
                    seen_names.add(f.name)
                except Exception as e:
                    logger.warning(f"Error reading backup stat for {f}: {e}")

    results.sort(key=lambda x: x["mtime"], reverse=True)
    for r in results:
        r.pop("mtime", None)

    return results


def restore_xui_backup(backup_filename: str) -> bool:
    """
    Safely restores a backup file over 3X-UI database and restarts 3x container.
    """
    filename = os.path.basename(backup_filename.strip())
    
    # Locate backup file across search dirs
    backup_path = None
    for candidate_dir in [get_backup_dir(), Path("/opt/vpn-private/backups/xui"), Path("/app/data/backups/xui"), Path("./data/backups/xui")]:
        candidate_file = candidate_dir / filename
        if candidate_file.is_file():
            backup_path = candidate_file
            break

    if not backup_path or not backup_path.exists():
        logger.error(f"Backup file not found for restore: {filename}")
        return False

    # Check validity of backup database
    try:
        test_conn = sqlite3.connect(str(backup_path))
        test_conn.execute("SELECT 1;").fetchone()
        test_conn.close()
    except Exception as e:
        logger.error(f"Cannot restore: invalid or corrupted backup database {backup_path}: {e}")
        return False

    restored = restore_db_via_xui_api(backup_path)
    if restored:
        return True

    db_path = resolve_xui_db_path()

    # Restore Method 1: Local filesystem overwrite
    if db_path:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            safety_path = db_path.with_suffix(".db.pre_restore")
            try:
                shutil.copy2(db_path, safety_path)
                logger.info(f"Created pre-restore safety copy at {safety_path}")
            except Exception as e:
                logger.warning(f"Could not create pre-restore safety copy: {e}")

        # Remove stale WAL/SHM files to prevent corruption upon restore
        wal_file = db_path.with_name(f"{db_path.name}-wal")
        shm_file = db_path.with_name(f"{db_path.name}-shm")
        if wal_file.exists():
            try:
                wal_file.unlink()
            except Exception as e:
                logger.warning(f"Failed to remove stale WAL file {wal_file}: {e}")
        if shm_file.exists():
            try:
                shm_file.unlink()
            except Exception as e:
                logger.warning(f"Failed to remove stale SHM file {shm_file}: {e}")

        try:
            shutil.copy2(backup_path, db_path)
            logger.info(f"Restored backup {backup_path} to {db_path}")
            restored = True
        except Exception as e:
            logger.error(f"Failed to overwrite {db_path} with backup {backup_path}: {e}")

    # Restore Method 2: Docker socket archive upload into container
    socket_restored = restore_db_to_docker_socket(backup_path)
    if socket_restored:
        restored = True

    if not restored:
        logger.error("Failed to restore X-UI database via filesystem or Docker socket.")
        return False

    # Restart 3X-UI service (auto-detects container or Host OS)
    restarted = restart_3x_container()
    if not restarted:
        logger.warning("Restored database, but 3X-UI restart did not complete cleanly. Check service or container status.")

    return True
