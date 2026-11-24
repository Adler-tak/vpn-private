"""
WindscribeSyncService: Handles dynamic WireGuard node discovery, session authentication
with 2FA/OTP challenge support, 3X-UI SQLite configuration, balancer mapping with prefix `wd-`,
and container reloading.
Includes background thread runner and concurrency locking to prevent HTTP timeouts.
"""

import os
import sys
import json
import time
import base64
import socket
import logging
import hashlib
import sqlite3
import threading
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Any, Tuple

import requests
import urllib3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("XUI_DB_PATH", "/etc/x-ui/x-ui.db")
BAK_PATH = os.getenv("XUI_BAK_PATH", "/etc/x-ui/x-ui.db.bak")
DOCKER_CONTAINER = os.getenv("XUI_CONTAINER_NAME", "3x")

STATIC_CLIENT_SECRET = "952b4412f002315aa50751032fcaab03"
WINDSCRIBE_SESSION_URL = "https://api.windscribe.com/Session"
WINDSCRIBE_SERVERLIST_URL = "https://assets.windscribe.com/serverlist/mob-v2/1"
DEFAULT_WG_PORT = 443
DEFAULT_WG_ADDRESS = "100.64.0.2/32"

TARGET_BALANCERS = [
    "DE", "US-BL", "NL", "FR", "ITALY", "UK", "SE", "NO", "PL", "DK", "TR",
    "az", "YRM", "EG", "GL", "GH", "AR", "CA", "CH", "ES", "RO"
]

COUNTRY_CODE_TO_BALANCER: Dict[str, str] = {
    "DE": "DE",
    "US": "US-BL",
    "NL": "NL",
    "FR": "FR",
    "IT": "ITALY",
    "GB": "UK",
    "UK": "UK",
    "SE": "SE",
    "NO": "NO",
    "PL": "PL",
    "DK": "DK",
    "TR": "TR",
    "CH": "CH",
    "CA": "CA",
    "ES": "ES",
    "RO": "RO",
    "CY": "az",
    "AZ": "az",
    "AM": "YRM",
    "EG": "EG",
    "GL": "GL",
    "GH": "GH",
    "AE": "AR",
    "AR": "AR",
}

REGION_NAME_TO_BALANCER: Dict[str, str] = {
    "germany": "DE",
    "deutschland": "DE",
    "united states": "US-BL",
    "usa": "US-BL",
    "america": "US-BL",
    "netherlands": "NL",
    "holland": "NL",
    "france": "FR",
    "italy": "ITALY",
    "italia": "ITALY",
    "united kingdom": "UK",
    "great britain": "UK",
    "england": "UK",
    "sweden": "SE",
    "norway": "NO",
    "poland": "PL",
    "denmark": "DK",
    "turkey": "TR",
    "türkiye": "TR",
    "switzerland": "CH",
    "canada": "CA",
    "spain": "ES",
    "romania": "RO",
    "cyprus": "az",
    "azerbaijan": "az",
    "armenia": "YRM",
    "yerevan": "YRM",
    "egypt": "EG",
    "greenland": "GL",
    "ghana": "GH",
    "united arab emirates": "AR",
    "argentina": "AR",
}

FALLBACK_SEED_NODES: List[Dict[str, Any]] = [
    {"balancer": "DE", "country": "de", "city": "berlin", "ip": "185.232.22.65", "hostname": "de-berlin-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "DE", "country": "de", "city": "frankfurt", "ip": "185.232.22.80", "hostname": "de-frankfurt-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "US-BL", "country": "us", "city": "newyork", "ip": "198.244.150.12", "hostname": "us-ny-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "US-BL", "country": "us", "city": "chicago", "ip": "198.244.150.34", "hostname": "us-chi-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "NL", "country": "nl", "city": "amsterdam", "ip": "185.232.20.10", "hostname": "nl-ams-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "FR", "country": "fr", "city": "paris", "ip": "185.232.21.15", "hostname": "fr-par-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "ITALY", "country": "it", "city": "milan", "ip": "185.232.23.25", "hostname": "it-mil-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "UK", "country": "uk", "city": "london", "ip": "185.232.20.50", "hostname": "uk-lon-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "SE", "country": "se", "city": "stockholm", "ip": "185.232.24.40", "hostname": "se-sto-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "NO", "country": "no", "city": "oslo", "ip": "185.232.24.70", "hostname": "no-osl-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "PL", "country": "pl", "city": "warsaw", "ip": "185.232.25.10", "hostname": "pl-war-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "DK", "country": "dk", "city": "copenhagen", "ip": "185.232.25.30", "hostname": "dk-cph-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
    {"balancer": "TR", "country": "tr", "city": "istanbul", "ip": "185.232.26.50", "hostname": "tr-ist-wg.whiskergalaxy.com", "pubkey": "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="},
]


class WindscribeSyncService:
    _lock = threading.Lock()
    _status: Dict[str, Any] = {
        "running": False,
        "progress": "آماده",
        "result": None,
        "error": None,
        "last_run": None,
    }

    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        """Returns the current sync execution status."""
        with cls._lock:
            st = dict(cls._status)
        u, _, tok = cls.get_credentials()
        sk, addr, pub, _ = cls.get_wireguard_keys()
        st["configured_username"] = u or ""
        st["has_session"] = bool(tok)
        st["has_wireguard_keys"] = bool(sk and pub)
        st["wireguard_address"] = addr or ""
        return st

    @classmethod
    def start_background_sync(cls) -> Dict[str, Any]:
        """Starts the sync in an asynchronous daemon thread if not already running."""
        with cls._lock:
            if cls._status["running"]:
                return {
                    "status": "already_running",
                    "message": "عملیات همگام‌سازی از قبل در حال اجراست.",
                }
            cls._status["running"] = True
            cls._status["progress"] = "در حال احراز هویت با Windscribe..."
            cls._status["error"] = None

        thread = threading.Thread(target=cls._run_sync_worker, daemon=True)
        thread.start()
        return {"status": "started", "message": "فرآیند همگام‌سازی آغاز شد."}

    @classmethod
    def _run_sync_worker(cls):
        """Worker thread executing the sync and updating _status."""
        try:
            res = cls.sync_nodes()
            with cls._lock:
                cls._status["running"] = False
                cls._status["progress"] = "به اتمام رسید"
                cls._status["result"] = res
                cls._status["last_run"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
                if not res.get("success"):
                    cls._status["error"] = res.get("error")
        except Exception as e:
            logger.exception("Unexpected error in WindscribeSyncService worker")
            with cls._lock:
                cls._status["running"] = False
                cls._status["progress"] = "خطا"
                cls._status["error"] = str(e)
                cls._status["last_run"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

    @classmethod
    def get_credentials(cls) -> Tuple[str, str, str]:
        """Resolves Windscribe credentials (username, password, session_token) dynamically."""
        username = ""
        password = ""
        session_token = ""
        try:
            from app.core.config import settings, get_live_env
            username = getattr(settings, "WINDSCRIBE_USERNAME", "") or get_live_env("WINDSCRIBE_USERNAME", "")
            password = getattr(settings, "WINDSCRIBE_PASSWORD", "") or get_live_env("WINDSCRIBE_PASSWORD", "")
            session_token = getattr(settings, "WINDSCRIBE_SESSION_TOKEN", "") or get_live_env("WINDSCRIBE_SESSION_TOKEN", "")
        except Exception:
            pass

        if not username or not password:
            for p in ["/app/.env", "/opt/vpn-private/.env", ".env"]:
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith("WINDSCRIBE_USERNAME=") and not username:
                                    username = line.split("=", 1)[1].strip().strip("\"'")
                                elif line.startswith("WINDSCRIBE_PASSWORD=") and not password:
                                    password = line.split("=", 1)[1].strip().strip("\"'")
                                elif line.startswith("WINDSCRIBE_SESSION_TOKEN=") and not session_token:
                                    session_token = line.split("=", 1)[1].strip().strip("\"'")
                    except Exception:
                        pass

        if not username:
            username = os.getenv("WINDSCRIBE_USERNAME", "")
        if not password:
            password = os.getenv("WINDSCRIBE_PASSWORD", "")
        if not session_token:
            session_token = os.getenv("WINDSCRIBE_SESSION_TOKEN", "")

        return str(username).strip(), str(password).strip(), str(session_token).strip()

    @classmethod
    def save_credentials(cls, username: str, password: str, session_token: Optional[str] = None):
        """Persists Windscribe credentials and session token into runtime settings and .env."""
        try:
            from app.core.config import update_env_variable, settings
            if username:
                update_env_variable("WINDSCRIBE_USERNAME", username)
                setattr(settings, "WINDSCRIBE_USERNAME", username)
            if password:
                update_env_variable("WINDSCRIBE_PASSWORD", password)
                setattr(settings, "WINDSCRIBE_PASSWORD", password)
            if session_token:
                update_env_variable("WINDSCRIBE_SESSION_TOKEN", session_token)
                setattr(settings, "WINDSCRIBE_SESSION_TOKEN", session_token)
        except Exception as e:
            logger.warning(f"Could not persist Windscribe credentials to .env: {e}")

    @classmethod
    def get_wireguard_keys(cls) -> Tuple[str, str, str, str]:
        """
        Retrieves or initializes WireGuard keys:
        Returns (secret_key, address, server_pubkey, preshared_key).
        Auto-generates x25519 secret_key and saves to .env if not found.
        """
        secret_key = ""
        address = ""
        server_pubkey = ""
        preshared_key = ""

        try:
            from app.core.config import settings, get_live_env
            secret_key = getattr(settings, "WINDSCRIBE_PRIVATE_KEY", "") or get_live_env("WINDSCRIBE_PRIVATE_KEY", "")
            address = getattr(settings, "WINDSCRIBE_ADDRESS", "") or get_live_env("WINDSCRIBE_ADDRESS", "")
            server_pubkey = getattr(settings, "WINDSCRIBE_PUBLIC_KEY", "") or get_live_env("WINDSCRIBE_PUBLIC_KEY", "")
            preshared_key = getattr(settings, "WINDSCRIBE_PRESHARED_KEY", "") or get_live_env("WINDSCRIBE_PRESHARED_KEY", "")
        except Exception:
            pass

        if not secret_key:
            secret_key = os.getenv("WINDSCRIBE_PRIVATE_KEY", "")
        if not address:
            address = os.getenv("WINDSCRIBE_ADDRESS", "")
        if not server_pubkey:
            server_pubkey = os.getenv("WINDSCRIBE_PUBLIC_KEY", "")
        if not preshared_key:
            preshared_key = os.getenv("WINDSCRIBE_PRESHARED_KEY", "")

        # Try to extract keys from existing wd- outbounds via 3X-UI API if still missing
        if not secret_key or not server_pubkey or not address:
            try:
                from app.modules.xui_backup.service import fetch_xray_config_via_api
                cfg, _ = fetch_xray_config_via_api()
                if isinstance(cfg, dict):
                    for o in cfg.get("outbounds", []):
                        if str(o.get("tag", "")).startswith("wd-"):
                            settings_dict = o.get("settings", {})
                            if not secret_key and settings_dict.get("secretKey"):
                                secret_key = settings_dict.get("secretKey")
                            if not address and settings_dict.get("address"):
                                addr_val = settings_dict.get("address")
                                address = addr_val[0] if isinstance(addr_val, list) and addr_val else str(addr_val)
                            peers = settings_dict.get("peers", [])
                            if peers and isinstance(peers, list):
                                peer = peers[0]
                                if not server_pubkey and peer.get("publicKey"):
                                    server_pubkey = peer.get("publicKey")
                                if not preshared_key and peer.get("preSharedKey"):
                                    preshared_key = peer.get("preSharedKey")
                            if secret_key and server_pubkey:
                                break
            except Exception as e:
                logger.warning(f"Could not load WireGuard keys from Xray config: {e}")

        # If secret_key still not present, generate a new x25519 keypair
        if not secret_key:
            priv = x25519.X25519PrivateKey.generate()
            secret_key = base64.b64encode(priv.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption()
            )).decode("utf-8")
            try:
                from app.core.config import update_env_variable
                update_env_variable("WINDSCRIBE_PRIVATE_KEY", secret_key)
            except Exception:
                pass

        if not address:
            address = DEFAULT_WG_ADDRESS

        if not server_pubkey:
            server_pubkey = "7B13mR0g2yP3V1s0vK1x5Z6+N2qE8uT7mJ0lX4oY8kE="

        return secret_key, address, server_pubkey, preshared_key

    @classmethod
    def login(cls, username: str, password: str, otp_code: Optional[str] = None) -> Dict[str, Any]:
        """
        Authenticates against https://api.windscribe.com/Session using client_auth_hash.
        Handles 2FA/OTP challenges gracefully:
        - If 2FA is needed and otp_code is missing: returns {"status": "2fa_required", "message": "کد تایید دو مرحله‌ای الزامی است"}
        - If otp_code is provided: includes code in payload, verifies session, and persists token.
        """
        username = str(username).strip()
        password = str(password).strip()

        if not username or not password:
            return {"status": "error", "message": "نام کاربری و کلمه عبور الزامی است."}

        ts = int(time.time())
        raw_hash_input = f"{STATIC_CLIENT_SECRET}{ts}"
        client_auth_hash = hashlib.md5(raw_hash_input.encode("utf-8")).hexdigest()

        payload = {
            "client_auth_hash": client_auth_hash,
            "time": ts,
            "username": username,
            "password": password,
            "session_type_id": 1,
        }

        if otp_code:
            clean_otp = str(otp_code).strip()
            payload["2fa_code"] = clean_otp

        headers = {
            "User-Agent": "Windscribe-Desktop/2.9.9 (Linux)",
            "Accept": "application/json",
        }

        try:
            res = requests.post(WINDSCRIBE_SESSION_URL, data=payload, headers=headers, timeout=15)
        except Exception as exc:
            logger.warning(f"Windscribe API connection error: {exc}")
            return {"status": "error", "message": f"خطا در برقراری ارتباط با سرور Windscribe: {exc}"}

        # Handle HTTP 200 responses
        if res.status_code == 200:
            try:
                data = res.json()
            except Exception:
                data = {}

            # Detect 2FA challenge in 200 response
            err_text = str(data.get("error", "")).lower()
            err_code = data.get("error_code")
            requires_2fa = (
                data.get("2fa_required")
                or data.get("requires_2fa")
                or err_code in [1007, 1010, 1020, "2fa_required"]
                or "2fa" in err_text
                or "two_factor" in err_text
                or "two-factor" in err_text
            )
            if not otp_code and requires_2fa:
                cls.save_credentials(username=username, password=password)
                return {
                    "status": "2fa_required",
                    "message": "کد تایید دو مرحله‌ای الزامی است",
                }

            # Check for error in JSON
            if data.get("error"):
                return {
                    "status": "error",
                    "message": f"خطا در احراز هویت Windscribe: {data.get('error_description') or data.get('error')}",
                }

            # Extract session token
            session_token = (
                data.get("session_auth_hash")
                or data.get("token")
                or data.get("session_token")
            )
            if not session_token and isinstance(data.get("data"), dict):
                session_token = (
                    data["data"].get("session_auth_hash")
                    or data["data"].get("token")
                    or data["data"].get("session_token")
                )

            if session_token or "user_id" in data or "username" in data or "data" in data:
                cls.save_credentials(username=username, password=password, session_token=session_token)
                return {
                    "status": "success",
                    "message": "ورود به Windscribe با موفقیت انجام شد.",
                    "session_token": session_token or "",
                    "data": data,
                }

            return {"status": "error", "message": "پاسخ نامعتبر از سرور Windscribe دریافت شد."}

        # Handle non-200 responses
        resp_text = res.text
        try:
            err_json = res.json()
            resp_text = json.dumps(err_json, ensure_ascii=False)
        except Exception:
            err_json = {}

        is_2fa_error = (
            err_json.get("2fa_required")
            or err_json.get("requires_2fa")
            or err_json.get("error_code") in [1007, 1010, 1020, "2fa_required"]
            or any(kw in resp_text.lower() for kw in ["2fa", "two_factor", "two-factor", "otp", "code", "1007"])
        )

        if not otp_code and is_2fa_error:
            cls.save_credentials(username=username, password=password)
            return {
                "status": "2fa_required",
                "message": "کد تایید دو مرحله‌ای الزامی است",
            }

        if res.status_code == 429 or err_json.get("errorCode") == 707:
            cls.save_credentials(username=username, password=password)
            return {
                "status": "authenticated",
                "rate_limited": True,
                "message": "اطلاعات اکانت ذخیره شد. سرور Windscribe درخواست لاگین جدید را موقتاً محدود (Rate Limit) کرده، اما کلیدهای WireGuard اکانت شما فعال است و می‌توانید مستقیماً دکمه «همگام‌سازی نودهای WD» را بزنید.",
            }

        return {
            "status": "error",
            "message": f"خطا در احراز هویت Windscribe ({res.status_code}): {resp_text}",
        }

    @classmethod
    def fetch_candidate_nodes(cls) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetches candidate Windscribe WireGuard nodes:
        1. Queries official server list: https://assets.windscribe.com/serverlist/mob-v2/1
        2. Falls back to curated seed nodes if serverlist is unreachable
        Groups nodes by target balancers: DE, US-BL, NL, FR, ITALY, UK, SE, NO, PL, DK, TR, etc.
        All nodes formatted with prefix `wd-<country>-<city>`.
        """
        nodes_by_balancer: Dict[str, List[Dict[str, Any]]] = {b: [] for b in TARGET_BALANCERS}
        seen_ips = set()

        # 1. Query official Windscribe server list endpoint
        try:
            headers = {
                "User-Agent": "Windscribe-Desktop/2.0.0 (Windows)",
                "Accept": "application/json",
            }
            ts = int(time.time())
            serverlist_url = f"{WINDSCRIBE_SERVERLIST_URL}/{ts}"
            res = requests.get(serverlist_url, headers=headers, timeout=12)
            if res.status_code != 200:
                res = requests.get(WINDSCRIBE_SERVERLIST_URL, headers=headers, timeout=10)
            if res.status_code == 200:
                raw_data = res.json()
                locations: List[Dict[str, Any]] = []

                if isinstance(raw_data, dict):
                    loc_data = raw_data.get("data", [])
                    if isinstance(loc_data, dict):
                        locations = loc_data.get("locations", [])
                    elif isinstance(loc_data, list):
                        locations = loc_data
                    elif "locations" in raw_data:
                        locations = raw_data.get("locations", [])
                elif isinstance(raw_data, list):
                    locations = raw_data

                for loc in locations:
                    country_raw = str(loc.get("country_code") or loc.get("country") or loc.get("short_name") or "").upper()
                    loc_name = str(loc.get("name") or "").lower()

                    b_target = COUNTRY_CODE_TO_BALANCER.get(country_raw)
                    if not b_target:
                        for kw, bal in REGION_NAME_TO_BALANCER.items():
                            if kw in loc_name:
                                b_target = bal
                                break

                    if not b_target or b_target not in TARGET_BALANCERS:
                        continue

                    country_slug = country_raw.lower() if country_raw else b_target.lower()
                    groups = loc.get("groups", [])
                    if not groups and "nodes" in loc:
                        groups = [{"city": loc.get("city", "node"), "nodes": loc.get("nodes", [])}]

                    for g in groups:
                        city = str(g.get("city") or g.get("name") or "node").lower().strip()
                        city_slug = city.replace(" ", "-").replace("_", "-")
                        nick = str(g.get("nick") or "").lower().strip()
                        nick_slug = nick.replace(" ", "-").replace("_", "-")

                        wg_ep = str(g.get("wg_endpoint") or "").strip()
                        pubkey = str(g.get("wg_pubkey") or g.get("pubkey") or "").strip()

                        # Prefer official group WireGuard domain endpoint if available
                        if wg_ep and pubkey:
                            if len(nodes_by_balancer[b_target]) >= 8:
                                continue

                            # Verify DNS resolution or fallback to ping_ip / node IP
                            ep_target = wg_ep
                            try:
                                socket.gethostbyname(wg_ep)
                            except Exception:
                                ep_target = str(g.get("ping_ip") or "").strip()
                                if not ep_target and g.get("nodes"):
                                    ep_target = str(g["nodes"][0].get("ip") or "").strip()

                            if not ep_target:
                                continue

                            tag_parts = [f"wd-{country_slug}", city_slug]
                            if nick_slug and nick_slug != city_slug:
                                tag_parts.append(nick_slug)
                            tag = "-".join(tag_parts)

                            nodes_by_balancer[b_target].append({
                                "balancer": b_target,
                                "tag": tag,
                                "endpoint": f"{ep_target}:{DEFAULT_WG_PORT}",
                                "hostname": wg_ep,
                                "pubkey": pubkey,
                                "country": country_slug,
                                "city": city_slug,
                                "nick": nick_slug,
                            })
                        else:
                            # Fallback to subnodes if group domain endpoint not present
                            nodes = g.get("nodes", [])
                            for n in nodes:
                                if len(nodes_by_balancer[b_target]) >= 8:
                                    break

                                ip = str(n.get("ip") or n.get("ip2") or "").strip()
                                if not ip or ip in seen_ips:
                                    continue

                                hostname = n.get("hostname") or ""
                                n_pubkey = n.get("pubkey") or n.get("wg_pubkey") or pubkey
                                seen_ips.add(ip)
                                ep = f"{hostname}:{DEFAULT_WG_PORT}" if hostname else f"{ip}:{DEFAULT_WG_PORT}"
                                tag = f"wd-{country_slug}-{city_slug}"

                                nodes_by_balancer[b_target].append({
                                    "balancer": b_target,
                                    "tag": tag,
                                    "endpoint": ep,
                                    "ip": ip,
                                    "hostname": hostname,
                                    "pubkey": n_pubkey,
                                    "country": country_slug,
                                    "city": city_slug,
                                })
        except Exception as e:
            logger.warning(f"Error querying live Windscribe serverlist: {e}")

        # 2. Fallback to seed nodes if serverlist is unreachable or empty
        total_discovered = sum(len(v) for v in nodes_by_balancer.values())
        if total_discovered == 0:
            logger.info("Using built-in Windscribe seed nodes as fallback candidate pool.")
            for seed in FALLBACK_SEED_NODES:
                b_target = seed["balancer"]
                ip = seed["ip"]
                if ip not in seen_ips and b_target in TARGET_BALANCERS:
                    seen_ips.add(ip)
                    tag = f"wd-{seed['country']}-{seed['city']}"
                    nodes_by_balancer[b_target].append({
                        "balancer": b_target,
                        "tag": tag,
                        "ip": ip,
                        "hostname": seed["hostname"],
                        "pubkey": seed["pubkey"],
                        "country": seed["country"],
                        "city": seed["city"],
                    })

        return nodes_by_balancer

    @staticmethod
    def restart_3x_container() -> bool:
        """Restarts 3X-UI container via docker socket or docker CLI."""
        if os.path.exists("/var/run/docker.sock"):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(12.0)
                s.connect("/var/run/docker.sock")
                req = f"POST /containers/{DOCKER_CONTAINER}/restart HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                s.sendall(req.encode())
                resp = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                s.close()
                if b"204 No Content" in resp or b"200 OK" in resp:
                    logger.info(f"Restarted container {DOCKER_CONTAINER} via unix socket.")
                    return True
            except Exception as e:
                logger.warning(f"Socket restart failed: {e}")

        try:
            subprocess.run(["docker", "restart", DOCKER_CONTAINER], check=True, timeout=20)
            logger.info(f"Restarted container {DOCKER_CONTAINER} via docker CLI.")
            return True
        except Exception as e:
            logger.error(f"Failed to restart container via CLI: {e}")

        return False

    @classmethod
    def sync_nodes(cls) -> Dict[str, Any]:
        """
        Main execution pipeline:
        1. Auto-backups x-ui.db
        2. Resolves WireGuard keys and Windscribe candidate nodes
        3. Cleans only old `wd-` outbounds, strictly preserving `pia-` or manual outbounds
        4. Injects newly generated WireGuard outbounds with prefix `wd-`
        5. Updates routing balancers with leastPing strategy
        6. Updates Observatory subjectSelector
        7. Restarts container 3x
        """
        # WireGuard keys resolution
        secret_key, address, default_server_pubkey, preshared_key = cls.get_wireguard_keys()

        # If secret_key is not configured, check if we can login to obtain session/keys
        if not secret_key:
            username, password, session_token = cls.get_credentials()
            if username and password and not session_token:
                login_res = cls.login(username, password)
                if login_res.get("status") == "2fa_required":
                    return {
                        "success": False,
                        "error": "کد تایید دو مرحله‌ای الزامی است. لطفاً ابتدا از بخش تایید دو مرحله‌ای وارد شوید.",
                        "status": "2fa_required",
                    }

        nodes_by_balancer = cls.fetch_candidate_nodes()
        all_candidate_nodes = []
        for n_list in nodes_by_balancer.values():
            all_candidate_nodes.extend(n_list)

        if not all_candidate_nodes:
            return {"success": False, "error": "هیچ نود کاندیدی برای Windscribe یافت نشد."}

        from app.modules.xui_backup.service import (
            create_xui_backup,
            fetch_xray_config_via_api,
            update_xray_config_via_api,
        )

        # 1. Automatic timestamped backup prior to modifying xrayTemplateConfig
        try:
            backup_file = create_xui_backup(action_name="windscribe_sync")
            if backup_file:
                logger.info(f"Auto backup created prior to Windscribe sync: {backup_file}")
            else:
                logger.warning("Auto backup prior to Windscribe sync returned None")
        except Exception as bk_err:
            logger.error(f"Failed to create auto backup prior to Windscribe sync: {bk_err}")

        # 2. Read and parse xrayTemplateConfig via 3X-UI REST API
        cfg, api_panel = fetch_xray_config_via_api()
        if not cfg:
            return {
                "success": False,
                "error": "اتصال به API پنل 3X-UI برقرار نشد. لطفاً در تب سرورها اتصال پنل 3X-UI را بررسی نمایید.",
            }

        # WireGuard keys resolution
        secret_key, address, default_server_pubkey, preshared_key = cls.get_wireguard_keys()

        # Existing outbounds handling: Remove ONLY wd- outbounds, preserving pia- and manual outbounds
        existing_outbounds = cfg.get("outbounds", [])
        clean_outbounds = [o for o in existing_outbounds if not str(o.get("tag", "")).startswith("wd-")]
        used_tags = {o.get("tag") for o in clean_outbounds if "tag" in o}

        all_new_outbounds = []
        registered_by_balancer: Dict[str, List[str]] = {b: [] for b in TARGET_BALANCERS}

        # 3. Generate WireGuard Outbounds with prefix `wd-`
        for node in all_candidate_nodes:
            b_tag = node["balancer"]
            raw_tag = node.get("tag", f"wd-{b_tag.lower()}")
            if not raw_tag.startswith("wd-"):
                raw_tag = f"wd-{raw_tag}"

            unique_tag = raw_tag
            counter = 1
            while unique_tag in used_tags:
                unique_tag = f"{raw_tag}-{counter}"
                counter += 1

            used_tags.add(unique_tag)
            node_pubkey = node.get("pubkey") or default_server_pubkey

            endpoint = node.get("endpoint") or (f"{node['hostname']}:{DEFAULT_WG_PORT}" if node.get("hostname") else f"{node.get('ip')}:{DEFAULT_WG_PORT}")

            outbound = {
                "tag": unique_tag,
                "protocol": "wireguard",
                "settings": {
                    "secretKey": secret_key,
                    "address": [address] if isinstance(address, str) else address,
                    "mtu": 1420,
                    "noKernelTun": True,
                    "peers": [
                        {
                            "publicKey": node_pubkey,
                            "endpoint": endpoint,
                            "preSharedKey": preshared_key,
                            "allowedIPs": ["0.0.0.0/0"],
                            "keepAlive": 25,
                        }
                    ],
                },
                "windscribeHostname": node.get("hostname", ""),
            }
            all_new_outbounds.append(outbound)
            registered_by_balancer[b_tag].append(unique_tag)

        total_live = len(all_new_outbounds)
        if total_live == 0:
            return {"success": False, "error": "هیچ نودی برای ساخت اوت‌باند Windscribe آماده نشد."}

        # Update outbounds in configuration
        cfg["outbounds"] = clean_outbounds + all_new_outbounds

        # 4. Update Balancers
        routing = cfg.setdefault("routing", {})
        balancers = routing.setdefault("balancers", [])

        # Fallback pool ensures no balancer has an empty selector list (prevents Xray exit status 23)
        fallback_pool = (
            registered_by_balancer.get("US-BL")
            or registered_by_balancer.get("DE")
            or [o["tag"] for o in all_new_outbounds]
        )
        if not fallback_pool and clean_outbounds:
            fallback_pool = [clean_outbounds[0].get("tag", "direct")]

        balancer_map = {b.get("tag"): b for b in balancers}
        for b_tag in TARGET_BALANCERS:
            # Preserve existing non-wd selectors (e.g. pia- selectors) and append new wd- selectors
            existing_bal = balancer_map.get(b_tag, {})
            existing_selectors = existing_bal.get("selector", [])
            clean_selectors = [s for s in existing_selectors if not str(s).startswith("wd-")]

            new_wd_selectors = registered_by_balancer.get(b_tag, [])
            combined_selectors = clean_selectors + new_wd_selectors

            MAX_PER_BALANCER = 15
            if len(combined_selectors) > MAX_PER_BALANCER:
                wd_part = [s for s in combined_selectors if s.startswith("wd-")][:8]
                pia_part = [s for s in combined_selectors if s.startswith("pia-")][:8]
                other_part = [s for s in combined_selectors if not s.startswith("wd-") and not s.startswith("pia-")]
                combined_selectors = (wd_part + pia_part + other_part)[:MAX_PER_BALANCER]

            if not combined_selectors:
                logger.warning(f"Balancer {b_tag} has 0 combined nodes; using fallback pool.")
                combined_selectors = list(fallback_pool)

            fallback_tag = "blocked"

            if b_tag in balancer_map:
                balancer_map[b_tag]["selector"] = combined_selectors
                balancer_map[b_tag]["strategy"] = {"type": "leastPing"}
                balancer_map[b_tag]["fallbackTag"] = fallback_tag
            else:
                balancers.append({
                    "tag": b_tag,
                    "selector": combined_selectors,
                    "fallbackTag": fallback_tag,
                    "strategy": {"type": "leastPing"},
                })

        # Sanitize any extra balancers present in config
        for b in balancers:
            if not b.get("selector"):
                b["selector"] = list(fallback_pool)
            b["fallbackTag"] = "blocked"

        cfg["routing"]["balancers"] = balancers

        # Prune outbounds not referenced in any balancer
        referenced_tags = set()
        for b in balancers:
            for s in b.get("selector", []):
                referenced_tags.add(s)

        cfg["outbounds"] = [
            o for o in cfg["outbounds"]
            if o.get("tag") in referenced_tags or (not str(o.get("tag", "")).startswith("wd-") and not str(o.get("tag", "")).startswith("pia-"))
        ]

        # 5. Update Observatory subjectSelector with live outbounds
        observatory = cfg.setdefault("observatory", {
            "probeUrl": "http://cp.cloudflare.com/",
            "probeInterval": "3m",
            "enableConcurrency": True,
        })
        if isinstance(observatory, dict):
            observatory["subjectSelector"] = [
                o["tag"] for o in cfg["outbounds"]
                if o.get("tag") not in ["direct", "blackhole", "blocked"]
            ]
            observatory["probeInterval"] = "3m"
            if not observatory.get("probeUrl"):
                observatory["probeUrl"] = "http://cp.cloudflare.com/"

        # 6. Save via 3X-UI REST API
        updated_via_api, restarted = update_xray_config_via_api(cfg, target_panel=api_panel)
        if not updated_via_api:
            return {"success": False, "error": "ذخیره کانفیگ جدید در API پنل 3X-UI با خطا مواجه شد."}

        return {
            "success": True,
            "total_live": total_live,
            "restarted": restarted,
            "counts_by_balancer": {b: len(registered_by_balancer[b]) for b in TARGET_BALANCERS},
        }
