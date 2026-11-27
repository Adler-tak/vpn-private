"""
PiaSyncService: Handles dynamic WireGuard node discovery, keypair generation,
port 1337 registration, 3X-UI SQLite configuration, and container reloading.
Includes background thread runner and concurrency locking to prevent HTTP timeouts.
"""

import os
import sys
import json
import time
import base64
import socket
import logging
import sqlite3
import threading
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Any

import requests
import urllib3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("XUI_DB_PATH", "/etc/x-ui/x-ui.db")
BAK_PATH = os.getenv("XUI_BAK_PATH", "/etc/x-ui/x-ui.db.bak")
DOCKER_CONTAINER = "3x"

TARGET_BALANCERS = [
    "US-BL", "DE", "NO", "ITALY", "FR", "PL", "az", "DK", 
    "YRM", "EG", "UK", "GL", "GH", "NL", "AR"
]


class PiaSyncService:
    _lock = threading.Lock()
    _status: Dict[str, Any] = {
        "running": False,
        "progress": "آماده",
        "result": None,
        "error": None,
        "last_run": None
    }

    @classmethod
    def get_target_balancers(cls) -> List[str]:
        """Dynamically loads active PIA balancer tags from database, falling back safely."""
        try:
            from app.services.balancer_location_service import BalancerLocationService
            tags = BalancerLocationService.get_active_tags_sync(provider="pia")
            if tags:
                return tags
        except Exception as e:
            logger.warning(f"Error reading dynamic balancer tags in PIA service: {e}")
        return TARGET_BALANCERS

    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        """Returns the current sync execution status."""
        return cls._status

    @classmethod
    def start_background_sync(cls) -> Dict[str, Any]:
        """Starts the sync in an asynchronous daemon thread if not already running."""
        with cls._lock:
            if cls._status["running"]:
                return {"status": "already_running", "message": "عملیات همگام‌سازی از قبل در حال اجراست."}
            cls._status["running"] = True
            cls._status["progress"] = "در حال احراز هویت با PIA..."
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
            logger.exception("Unexpected error in PiaSyncService worker")
            with cls._lock:
                cls._status["running"] = False
                cls._status["progress"] = "خطا"
                cls._status["error"] = str(e)
                cls._status["last_run"] = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

    @classmethod
    def get_credentials(cls) -> tuple[str, str]:
        """Resolves PIA credentials dynamically from settings, live .env file, or OS environment."""
        username = ""
        password = ""
        try:
            from app.core.config import settings, get_live_env
            username = getattr(settings, "PIA_USERNAME", "") or get_live_env("PIA_USERNAME", "")
            password = getattr(settings, "PIA_PASSWORD", "") or get_live_env("PIA_PASSWORD", "")
        except Exception:
            pass
        if not username or not password:
            for p in ["/app/.env", "/opt/vpn-private/.env", ".env"]:
                if os.path.exists(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line.startswith("PIA_USERNAME=") and not username:
                                    username = line.split("=", 1)[1].strip().strip("\"'")
                                elif line.startswith("PIA_PASSWORD=") and not password:
                                    password = line.split("=", 1)[1].strip().strip("\"'")
                    except Exception:
                        pass
        if not username:
            username = os.getenv("PIA_USERNAME", "")
        if not password:
            password = os.getenv("PIA_PASSWORD", "")
        return str(username).strip(), str(password).strip()

    @classmethod
    def save_credentials(cls, username: str, password: str) -> None:
        """Persists PIA credentials into runtime settings and .env file."""
        try:
            from app.core.config import settings, update_env_variable
            if username is not None:
                update_env_variable("PIA_USERNAME", username)
                setattr(settings, "PIA_USERNAME", username)
                os.environ["PIA_USERNAME"] = username
            if password is not None:
                update_env_variable("PIA_PASSWORD", password)
                setattr(settings, "PIA_PASSWORD", password)
                os.environ["PIA_PASSWORD"] = password
        except Exception as e:
            logger.warning(f"Could not persist PIA credentials to .env: {e}")

    @classmethod
    def login(cls, username: str, password: str) -> Dict[str, Any]:
        """Authenticates against PIA v2 token endpoint and saves credentials on success."""
        clean_user = (username or "").strip()
        clean_pass = (password or "").strip()
        if not clean_user or not clean_pass:
            return {"status": "error", "message": "لطفاً نام کاربری (مثلاً p1234567) و رمز عبور PIA را وارد نمایید."}

        url = "https://www.privateinternetaccess.com/api/client/v2/token"
        headers = {
            "User-Agent": "PIA-Desktop/3.3.1 (Linux)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            res = requests.post(
                url,
                json={"username": clean_user, "password": clean_pass},
                headers=headers,
                timeout=15,
            )
            if res.status_code == 200 and res.json().get("token"):
                cls.save_credentials(clean_user, clean_pass)
                with cls._lock:
                    cls._status["error"] = None
                return {
                    "status": "authenticated",
                    "message": "ورود به حساب PIA با موفقیت تایید و ذخیره شد. اکنون می‌توانید همگام‌سازی نودها را اجرا کنید.",
                }
            cls.save_credentials(clean_user, clean_pass)
            return {
                "status": "error",
                "message": f"خطا در احراز هویت PIA (کد {res.status_code}): نام کاربری یا رمز عبور اشتباه است.",
            }
        except Exception as exc:
            cls.save_credentials(clean_user, clean_pass)
            return {
                "status": "error",
                "message": f"اطلاعات ذخیره شد اما اتصال به سرور احراز هویت PIA برقرار نشد: {exc}",
            }

    @classmethod
    def get_pia_token(cls) -> Optional[str]:
        username, password = cls.get_credentials()
        if not username or not password:
            logger.error("PIA_USERNAME or PIA_PASSWORD is not set in environment or .env file.")
            return None
        url = "https://www.privateinternetaccess.com/api/client/v2/token"
        headers = {
            "User-Agent": "PIA-Desktop/3.3.1 (Linux)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            res = requests.post(
                url,
                json={"username": username, "password": password},
                headers=headers,
                timeout=12,
            )
            if res.status_code == 200:
                return res.json().get("token")
            logger.error(f"PIA Auth failed: {res.status_code} - {res.text}")
        except Exception as exc:
            logger.error(f"PIA Auth error: {exc}")
        return None

    @classmethod
    def fetch_candidate_nodes(cls, token: str) -> Dict[str, List[Dict]]:
        active_balancers = cls.get_target_balancers()
        nodes_by_balancer = {b: [] for b in active_balancers}
        seen_ips = set()

        # 1. Load known nodes from 3X-UI API
        cfg_existing = None
        try:
            from app.modules.xui_backup.service import fetch_xray_config_via_api
            cfg_existing, _ = fetch_xray_config_via_api()
        except Exception as e:
            logger.warning(f"Could not pre-load existing PIA nodes: {e}")

        if isinstance(cfg_existing, dict):
            try:
                outbounds = {o.get("tag"): o for o in cfg_existing.get("outbounds", []) if "tag" in o}
                for b in cfg_existing.get("routing", {}).get("balancers", []):
                    b_tag = b.get("tag")
                    if b_tag in active_balancers:
                        for s in b.get("selector", []):
                            o = outbounds.get(s)
                            if o:
                                peers = o.get("settings", {}).get("peers", [])
                                if peers:
                                    endpoint = peers[0].get("endpoint", "")
                                    ip = endpoint.split(":")[0] if endpoint else ""
                                    pubkey = peers[0].get("publicKey", "")
                                    if ip and ip not in seen_ips:
                                        if len(nodes_by_balancer[b_tag]) >= 8:
                                            continue
                                        seen_ips.add(ip)
                                        nodes_by_balancer[b_tag].append({
                                            "balancer": b_tag,
                                            "tag": s,
                                            "ip": ip,
                                            "pubkey": pubkey,
                                            "piaHostname": o.get("piaHostname", "")
                                        })
            except Exception as e:
                logger.warning(f"Could not parse existing PIA nodes from config: {e}")

        # 2. Query PIA serverlist v4 for new live nodes
        try:
            s_url = "https://serverlist.piaservers.net/vpninfo/servers/v4"
            headers = {"Authorization": f"Bearer {token}", "User-Agent": "PIA-Desktop/3.3.1 (Linux)"}
            s_res = requests.get(s_url, headers=headers, timeout=15)
            if s_res.status_code == 200:
                json_line = s_res.text.strip().split("\n")[0]
                data = json.loads(json_line)

                region_to_balancer = {
                    "poland": "PL", "cyprus": "az", "denmark": "DK", "yerevan": "YRM",
                    "egypt": "EG", "greenland": "GL", "ghana": "GH", "no": "NO",
                    "france": "FR", "ae": "AR", "sweden": "SE", "turkey": "TR",
                    "japan": "JP", "singapore": "SG", "finland": "FI", "austria": "AT",
                    "australia": "AU", "belgium": "BE", "brazil": "BR", "bulgaria": "BG",
                    "czech": "CZ", "greece": "GR", "hungary": "HU", "ireland": "IE",
                    "israel": "IL", "india": "IN", "mexico": "MX", "newzealand": "NZ",
                    "portugal": "PT", "ukraine": "UA", "southafrica": "ZA", "iceland": "IS",
                    "switzerland": "CH", "canada": "CA", "spain": "ES", "romania": "RO",
                    "hongkong": "HK",
                }

                for reg in data.get("regions", []):
                    rid = reg.get("id", "")
                    country = str(reg.get("country", "")).upper()

                    b_target = None
                    if rid in region_to_balancer:
                        b_target = region_to_balancer[rid]
                    elif country == "US" or rid.startswith("us"):
                        b_target = "US-BL"
                    elif country == "DE" or rid in ["de_berlin", "de-frankfurt"]:
                        b_target = "DE"
                    elif country == "IT" or rid in ["italy", "it-naples"]:
                        b_target = "ITALY"
                    elif country == "GB" or rid.startswith("uk"):
                        b_target = "UK"
                    elif country == "NL" or rid.startswith("nl"):
                        b_target = "NL"
                    elif country in active_balancers:
                        b_target = country

                    if b_target and b_target in active_balancers:
                        for s in reg.get("servers", {}).get("wg", []):
                            ip = s.get("ip")
                            if ip and ip not in seen_ips:
                                if len(nodes_by_balancer[b_target]) >= 8:
                                    continue
                                seen_ips.add(ip)
                                cn = s.get("cn", "")
                                tag = f"pia-{rid}-{cn}".lower().replace("_", "-")
                                nodes_by_balancer[b_target].append({
                                    "balancer": b_target,
                                    "tag": tag,
                                    "ip": ip,
                                    "pubkey": s.get("pubkey", ""),
                                    "piaHostname": cn
                                })
                logger.info(f"Loaded {len(seen_ips)} live PIA candidate nodes across {len(active_balancers)} active balancers.")
        except Exception as e:
            logger.warning(f"Error fetching PIA serverlist additions: {e}")

        return nodes_by_balancer

    @staticmethod
    def register_node(node: Dict, token: str) -> Optional[Dict]:
        priv = x25519.X25519PrivateKey.generate()
        pub = priv.public_key()
        priv_b64 = base64.b64encode(priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption()
        )).decode("utf-8")
        pub_b64 = base64.b64encode(pub.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )).decode("utf-8")

        url = f"https://{node['ip']}:1337/addKey"
        try:
            # 6.0s timeout ensures overseas and high-latency nodes register reliably
            res = requests.get(url, params={"pt": token, "pubkey": pub_b64}, verify=False, timeout=6.0)
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "OK":
                    peer_ip = data.get("peer_ip")
                    server_key = data.get("server_key") or node["pubkey"]
                    if peer_ip and server_key:
                        return {
                            "balancer": node["balancer"],
                            "tag": node["tag"],
                            "ip": node["ip"],
                            "peer_ip": peer_ip,
                            "server_key": server_key,
                            "priv_key": priv_b64,
                            "piaHostname": node.get("piaHostname", "")
                        }
        except Exception:
            pass
        return None

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
                    logger.info("Restarted container 3x via unix socket.")
                    return True
            except Exception as e:
                logger.warning(f"Socket restart failed: {e}")

        try:
            subprocess.run(["docker", "restart", DOCKER_CONTAINER], check=True, timeout=20)
            logger.info("Restarted container 3x via docker CLI.")
            return True
        except Exception as e:
            logger.error(f"Failed to restart container via CLI: {e}")

        return False

    @classmethod
    def sync_nodes(cls) -> Dict:
        """Main execution method, returning status summary dictionary."""
        token = cls.get_pia_token()
        if not token:
            return {"success": False, "error": "احراز هویت PIA با خطا مواجه شد."}

        nodes_by_b = cls.fetch_candidate_nodes(token)
        all_tasks = []
        for n_list in nodes_by_b.values():
            all_tasks.extend(n_list)

        if not all_tasks:
            return {"success": False, "error": "هیچ نود کاندیدی یافت نشد."}

        active_balancers = cls.get_target_balancers()
        registered_by_balancer = {b: [] for b in active_balancers}
        all_outbounds = []
        used_tags = set()

        from app.modules.xui_backup.service import (
            create_xui_backup,
            fetch_xray_config_via_api,
            update_xray_config_via_api,
        )

        # Automatic timestamped backup before reading/modifying xrayTemplateConfig
        try:
            backup_file = create_xui_backup(action_name="pia_sync")
            if backup_file:
                logger.info(f"Auto backup created prior to PIA sync: {backup_file}")
            else:
                logger.warning("Auto backup prior to PIA sync returned None")
        except Exception as bk_err:
            logger.error(f"Failed to create auto backup prior to PIA sync: {bk_err}")

        # Fetch live xrayTemplateConfig via 3X-UI REST API
        cfg, api_panel = fetch_xray_config_via_api()
        if not cfg:
            return {
                "success": False,
                "error": "اتصال به API پنل 3X-UI برقرار نشد. لطفاً در تب سرورها اتصال پنل 3X-UI را بررسی نمایید.",
            }

        existing_outbounds = cfg.get("outbounds", [])
        clean_outbounds = [o for o in existing_outbounds if not str(o.get("tag", "")).startswith("pia-")]
        for o in clean_outbounds:
            used_tags.add(o.get("tag"))

        # Run 35 parallel workers for rapid execution (completes in ~4-6 seconds)
        with ThreadPoolExecutor(max_workers=35) as executor:
            futures = {executor.submit(cls.register_node, node, token): node for node in all_tasks}
            for f in as_completed(futures):
                res = f.result()
                if res:
                    b_tag = res["balancer"]
                    raw_tag = res["tag"]

                    unique_tag = raw_tag
                    counter = 1
                    while unique_tag in used_tags:
                        unique_tag = f"{raw_tag}-{counter}"
                        counter += 1

                    used_tags.add(unique_tag)

                    outbound = {
                        "tag": unique_tag,
                        "protocol": "wireguard",
                        "settings": {
                            "secretKey": res["priv_key"],
                            "address": [f"{res['peer_ip']}/32"],
                            "mtu": 1420,
                            "noKernelTun": True,
                            "peers": [
                                {
                                    "publicKey": res["server_key"],
                                    "endpoint": f"{res['ip']}:1337",
                                    "allowedIPs": ["0.0.0.0/0"],
                                    "keepAlive": 25
                                }
                            ]
                        },
                        "piaHostname": res.get("piaHostname", "")
                    }
                    all_outbounds.append(outbound)
                    registered_by_balancer[b_tag].append(unique_tag)

        total_live = sum(len(v) for v in registered_by_balancer.values())
        if total_live == 0:
            return {"success": False, "error": "هیچ نودی با سرورهای PIA فعال نشد."}

        routing = cfg.setdefault("routing", {})
        balancers = routing.setdefault("balancers", [])
        cfg["outbounds"] = clean_outbounds + all_outbounds

        # Fallback pool ensures no balancer ever has an empty selector list (prevents Xray exit status 23)
        fallback_pool = (
            registered_by_balancer.get("US-BL")
            or registered_by_balancer.get("DE")
            or [o["tag"] for o in all_outbounds]
        )
        if not fallback_pool and clean_outbounds:
            fallback_pool = [clean_outbounds[0].get("tag", "direct")]

        balancer_map = {b.get("tag"): b for b in balancers}
        for b_tag in active_balancers:
            existing_bal = balancer_map.get(b_tag, {})
            existing_selectors = existing_bal.get("selector", [])
            clean_selectors = [s for s in existing_selectors if not str(s).startswith("pia-")]
            new_pia_selectors = registered_by_balancer.get(b_tag, [])
            combined_selectors = clean_selectors + new_pia_selectors

            MAX_PER_BALANCER = 15
            if len(combined_selectors) > MAX_PER_BALANCER:
                wd_part = [s for s in combined_selectors if s.startswith("wd-")][:8]
                pia_part = [s for s in combined_selectors if s.startswith("pia-")][:8]
                other_part = [s for s in combined_selectors if not s.startswith("wd-") and not s.startswith("pia-")]
                combined_selectors = (wd_part + pia_part + other_part)[:MAX_PER_BALANCER]

            if not combined_selectors:
                logger.warning(
                    f"Balancer {b_tag} had 0 registered nodes; using fallback pool to prevent empty selector list crash."
                )
                combined_selectors = list(fallback_pool)

            # fallbackTag set to blocked to prevent unintended routing leaks
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
                    "strategy": {"type": "leastPing"}
                })

        # Sanitize any other balancers present in config
        for b in balancers:
            if not b.get("selector"):
                b["selector"] = fallback_pool
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

        # Update Observatory subjectSelector so all live outbounds receive ping probes
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

        # Save via 3X-UI REST API
        updated_via_api, restarted = update_xray_config_via_api(cfg, target_panel=api_panel)
        if not updated_via_api:
            return {"success": False, "error": "ذخیره کانفیگ جدید در API پنل 3X-UI با خطا مواجه شد."}

        return {
            "success": True,
            "total_live": total_live,
            "restarted": restarted,
            "counts_by_balancer": {b: len(registered_by_balancer.get(b, [])) for b in active_balancers}
        }
