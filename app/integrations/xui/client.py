"""Low-level asynchronous HTTP client for 3x-ui v3.11 panels."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import struct
import time
import urllib.parse
from typing import Any, Dict, List, Optional
import httpx
from app.core.exceptions import PanelAuthError, PanelError

logger = logging.getLogger(__name__)


def generate_totp_code(secret: Optional[str]) -> Optional[str]:
    """Generate 6-digit RFC 6238 TOTP code from a Base32 secret string or return secret if already 6-digit code."""
    if not secret:
        return None
    cleaned = secret.strip().replace(" ", "").upper()
    if not cleaned:
        return None
    if cleaned.isdigit() and len(cleaned) == 6:
        return cleaned

    missing_padding = len(cleaned) % 8
    if missing_padding:
        cleaned += "=" * (8 - missing_padding)

    try:
        key = base64.b32decode(cleaned, casefold=True)
    except Exception:
        return secret.strip()

    intervals_no = int(time.time()) // 30
    msg = struct.pack(">Q", intervals_no)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[19] & 15
    h_code = (struct.unpack(">I", h[o:o+4])[0] & 0x7fffffff) % 1000000
    return str(h_code).zfill(6)


def normalize_panel_base_url(url: str, uri_path: Optional[str] = None) -> str:
    """Normalize 3x-ui panel URL by stripping UI paths (/panel, /panel/inbounds) and trailing slashes, and cleanly attaching uri_path if provided."""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    url = url.rstrip("/")
    for suffix in ["/panel/inbounds", "/inbounds", "/panel"]:
        if url.endswith(suffix):
            url = url[:-len(suffix)].rstrip("/")

    if uri_path:
        clean_path = uri_path.strip()
        if clean_path and clean_path != "/":
            if not clean_path.startswith("/"):
                clean_path = "/" + clean_path
            if not clean_path.endswith("/"):
                clean_path = clean_path + "/"

            # Check if url already contains or ends with uri_path
            parsed = urllib.parse.urlparse(url)
            parsed_path = parsed.path.rstrip("/") + "/"
            if not parsed_path.endswith(clean_path):
                url = url.rstrip("/") + clean_path.rstrip("/")

    return url


class ThreeXUIClient:
    """Async HTTP Client interfacing directly with 3x-ui v3.11 REST API."""

    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        api_token: Optional[str] = None,
        login_secret: Optional[str] = None,
        otp_secret: Optional[str] = None,
        timeout: float = 12.0,
        verify_ssl: bool = False,
        uri_path: Optional[str] = None,
    ):
        self.uri_path = uri_path
        if self.uri_path:
            clean_uri = self.uri_path.strip()
            if clean_uri and clean_uri != "/":
                if not clean_uri.startswith("/"):
                    clean_uri = "/" + clean_uri
                if not clean_uri.endswith("/"):
                    clean_uri = clean_uri + "/"
                self.uri_path = clean_uri
            else:
                self.uri_path = None

        self.base_url = normalize_panel_base_url(base_url, uri_path=self.uri_path)
        self.username = username or ""
        self.password = password or ""
        self.api_token = api_token
        self.login_secret = (login_secret or "").strip()
        self.otp_secret = (otp_secret or "").strip()
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self._client: Optional[httpx.AsyncClient] = None
        self._is_authenticated = False
        self._csrf_token: Optional[str] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            parsed = urllib.parse.urlparse(self.base_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            default_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": origin,
                "Referer": self.base_url + "/",
                "X-Requested-With": "XMLHttpRequest",
            }
            if self._csrf_token:
                default_headers["X-CSRF-Token"] = self._csrf_token
            if self.api_token:
                default_headers["Authorization"] = f"Bearer {self.api_token}"

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                verify=self.verify_ssl,
                follow_redirects=True,
                headers=default_headers,
            )
        else:
            if self.api_token and "Authorization" not in self._client.headers:
                self._client.headers["Authorization"] = f"Bearer {self.api_token}"
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
            self._is_authenticated = False

    async def login(self) -> bool:
        """Authenticate with the 3x-ui panel."""
        client = await self._get_client()

        # If API token is configured, test directly using Authorization Bearer header without needing username/password
        if self.api_token:
            headers = {"Authorization": f"Bearer {self.api_token}"}
            client.headers.update(headers)
            try:
                # 1. Probe server status endpoint
                resp = await client.get("/panel/api/server/status", headers=headers)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("success", False):
                            self._is_authenticated = True
                            return True
                    except Exception:
                        pass

                # 2. Fallback probe: inbounds list endpoint
                resp_inb = await client.get("/panel/api/inbounds/list", headers=headers)
                if resp_inb.status_code == 200:
                    try:
                        data_inb = resp_inb.json()
                        if isinstance(data_inb, dict) and data_inb.get("success", False):
                            self._is_authenticated = True
                            return True
                    except Exception:
                        pass

                # If token was rejected and no fallback credentials available
                if not self.username or not self.password or self.username == "token_user" or self.password == "API_TOKEN_AUTH":
                    raise PanelAuthError(f"Bearer token authentication failed on {self.base_url}: HTTP {resp.status_code}")
                logger.warning(f"Bearer token auth failed for {self.base_url}: HTTP {resp.status_code}, falling back to session login.")
            except Exception as e:
                if not self.username or not self.password or self.username == "token_user" or self.password == "API_TOKEN_AUTH":
                    if isinstance(e, (PanelAuthError, PanelError)):
                        raise
                    raise PanelAuthError(f"Bearer token authentication failed for {self.base_url}: {e}") from e
                logger.warning(f"Bearer token auth failed for {self.base_url}: {e}, trying session login.")

        # Modern 3x-ui CSRF token detection:
        # Fetch the login page first to acquire session cookie and CSRF token if protected
        try:
            get_resp = await client.get(self.base_url + "/")
            if get_resp.status_code == 200:
                match = re.search(r'name=["\']csrf-token["\'] content=["\']([^"\']+)["\']', get_resp.text)
                if match:
                    self._csrf_token = match.group(1)
                    client.headers["X-CSRF-Token"] = self._csrf_token
                    logger.debug(f"Acquired CSRF token for {self.base_url}")
        except Exception as csrf_err:
            logger.debug(f"Notice: CSRF probe on {self.base_url}: {csrf_err}")

        # Fallback to session cookie login
        login_url = "/login"
        payload = {"username": self.username, "password": self.password}

        # Resolve OTP / 2FA code and static LoginSecret
        active_secret = self.otp_secret or self.login_secret
        if active_secret:
            otp_val = generate_totp_code(active_secret)
            # If a static login secret was provided, supply it directly
            if self.login_secret:
                payload["loginSecret"] = self.login_secret
            # If TOTP code was resolved (either generated from base32 or 6-digit code)
            if otp_val:
                payload["twoFactorCode"] = otp_val
                payload["code"] = otp_val
                # If loginSecret wasn't explicitly set or is identical to active_secret, populate for legacy 3x-ui
                if "loginSecret" not in payload or payload["loginSecret"] == active_secret:
                    payload["loginSecret"] = otp_val
            logger.info(f"Submitting credentials for 3x-ui login to {self.base_url}")
        try:
            # 3x-ui accepts both JSON and form data
            resp = await client.post(login_url, json=payload)
            is_html = resp.text.strip().startswith("<!DOCTYPE") or "text/html" in resp.headers.get("content-type", "")
            if resp.status_code != 200 or is_html:
                resp = await client.post(login_url, data=payload)

            if resp.status_code == 403:
                # 3x-ui burst rate-limit or concurrent session lock: wait 1.5s and retry once
                logger.info(f"Notice: HTTP 403 on 3x-ui login to {self.base_url}, retrying with 1.5s backoff...")
                await asyncio.sleep(1.5)
                resp = await client.post(login_url, json=payload)
                if resp.status_code != 200:
                    resp = await client.post(login_url, data=payload)

            if resp.status_code in (500, 502, 503, 504):
                raise PanelError(f"Panel server error HTTP {resp.status_code} on {self.base_url}: {resp.text[:200]}")
            if resp.status_code == 403:
                raise PanelAuthError(f"Access forbidden (HTTP 403) on {self.base_url}: {resp.text[:200]}")

            try:
                data = resp.json() if resp.status_code == 200 else {}
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}

            if resp.status_code == 200 and data.get("success", False):
                self._is_authenticated = True
                logger.info(f"Successfully authenticated with 3x-ui panel: {self.base_url}")
                return True
            else:
                msg = data.get("msg") or resp.text[:200]
                raise PanelAuthError(f"Login failed on {self.base_url}: {msg}")
        except httpx.RequestError as exc:
            raise PanelError(f"Connection error to {self.base_url}: {exc}") from exc

    async def _request(
        self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None, retry_login: bool = True
    ) -> Dict[str, Any]:
        """Execute authenticated request with auto re-login and HTML session redirect detection."""
        client = await self._get_client()
        if not self._is_authenticated and not self.api_token:
            await self.login()

        url = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        req_headers = {}
        if self.api_token:
            req_headers["Authorization"] = f"Bearer {self.api_token}"
            if "Authorization" not in client.headers:
                client.headers["Authorization"] = f"Bearer {self.api_token}"

        try:
            resp = await client.request(method, url, json=json_data, headers=req_headers if req_headers else None)

            # Check for server failures (500, 502, 503, 504) - don't treat as session expiration!
            if resp.status_code >= 500:
                raise PanelError(f"Request {method} {url} returned HTTP {resp.status_code}: {resp.text[:300]}")

            # Check if session expired (HTTP 401, 302, or HTML redirect/login page)
            is_html_login = (
                resp.status_code in (401, 302)
                or (
                    resp.status_code == 200
                    and (
                        resp.text.strip().lower().startswith("<!doctype")
                        or resp.text.strip().lower().startswith("<html")
                        or "text/html" in resp.headers.get("content-type", "")
                    )
                )
            )
            if (resp.status_code in (401, 302) or is_html_login) and retry_login:
                logger.info(f"Session expired on {self.base_url}, re-authenticating...")
                self._is_authenticated = False
                await self.login()
                return await self._request(method, endpoint, json_data=json_data, retry_login=False)

            if resp.status_code in (401, 403):
                raise PanelAuthError(f"Request {method} {url} returned HTTP {resp.status_code}: {resp.text[:300]}")

            if resp.status_code != 200:
                raise PanelError(f"Request {method} {url} returned HTTP {resp.status_code}: {resp.text[:300]}")

            try:
                result = resp.json()
            except Exception as json_err:
                if retry_login:
                    logger.info(f"Non-JSON response from {url}, attempting session re-auth...")
                    self._is_authenticated = False
                    await self.login()
                    return await self._request(method, endpoint, json_data=json_data, retry_login=False)
                else:
                    raise PanelError(f"Panel returned invalid non-JSON output on {url}: {json_err}")

            if not isinstance(result, dict):
                raise PanelError(f"Panel returned non-dict JSON output on {url}: {type(result).__name__}")

            if not result.get("success", True):
                msg = str(result.get("msg") or "Unknown error")
                raise PanelError(f"Panel returned failure on {url}: {msg}")
            return result
        except httpx.RequestError as exc:
            raise PanelError(f"Request failure {method} {url} on {self.base_url}: {exc}") from exc

    # Status endpoints
    async def get_status(self) -> Dict[str, Any]:
        return await self._request("GET", "/panel/api/server/status")

    async def get_default_settings(self) -> Dict[str, Any]:
        """Fetch panel default settings including subURI, subDomain, subPort, etc."""
        try:
            res = await self._request("POST", "/panel/api/setting/defaultSettings")
            return res.get("obj", {})
        except Exception as e:
            logger.debug(f"Could not fetch defaultSettings: {e}")
            return {}

    async def get_xray_version(self) -> Dict[str, Any]:
        return await self._request("GET", "/panel/api/server/getXrayVersion")

    # Inbounds endpoints
    async def get_inbounds(self) -> List[Dict[str, Any]]:
        res = await self._request("GET", "/panel/api/inbounds/list")
        obj = res.get("obj")
        if isinstance(obj, list):
            return obj
        return []

    async def get_inbound(self, inbound_id: int) -> Optional[Dict[str, Any]]:
        res = await self._request("GET", f"/panel/api/inbounds/get/{inbound_id}")
        obj = res.get("obj")
        return obj if isinstance(obj, dict) else None

    # Client endpoints
    async def add_client(self, inbound_id: int, client_spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add client to specified inbound.
        Supports both modern 3x-ui (/panel/api/clients/add) and legacy fallback (/panel/api/inbounds/addClient).
        """
        return await self.add_clients_to_inbounds([inbound_id], client_spec)

    async def add_clients_to_inbounds(self, inbound_ids: List[int], client_spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add client to multiple specified inbounds simultaneously.
        Uses modern /panel/api/clients/add with inboundIds list, or falls back to adding to each inbound sequentially.
        """
        if not inbound_ids:
            return {"success": True, "msg": "No inbounds provided"}

        clean_inbound_ids = [int(i) for i in inbound_ids if i is not None]
        if not clean_inbound_ids:
            return {"success": True, "msg": "No valid inbounds provided"}

        modern_payload = {
            "inboundIds": clean_inbound_ids,
            "client": client_spec,
        }
        try:
            return await self._request("POST", "/panel/api/clients/add", json_data=modern_payload)
        except PanelError as exc:
            if "404" in str(exc):
                logger.info(
                    "Route /panel/api/clients/add returned 404, falling back to legacy /panel/api/inbounds/addClient"
                )
                results = []
                base_email = str(client_spec.get("email") or "")
                for idx, inb_id in enumerate(clean_inbound_ids):
                    spec_copy = dict(client_spec)
                    if idx > 0 and base_email:
                        if "@" in base_email:
                            local_p, dom_p = base_email.split("@", 1)
                            spec_copy["email"] = f"{local_p}_inb{inb_id}@{dom_p}"
                        else:
                            spec_copy["email"] = f"{base_email}_inb{inb_id}"
                    settings_payload = json.dumps({"clients": [spec_copy]})
                    payload = {"id": inb_id, "settings": settings_payload}
                    try:
                        res = await self._request("POST", "/panel/api/inbounds/addClient", json_data=payload)
                        results.append(res)
                    except Exception as loop_e:
                        logger.warning(f"Failed to add client to inbound {inb_id}: {loop_e}")
                if not results:
                    raise PanelError(f"Failed to add client to inbounds {clean_inbound_ids} via legacy endpoint")
                return {"success": True, "msg": f"Added to {len(results)} inbounds via legacy endpoint"}
            raise

    async def update_client(self, inbound_id: int, client_uuid: str, client_spec: Dict[str, Any]) -> Dict[str, Any]:
        """Update client parameters."""
        client_email = client_spec.get("email")
        if client_email:
            try:
                safe_email = urllib.parse.quote(client_email)
                return await self._request("POST", f"/panel/api/clients/update/{safe_email}", json_data=client_spec)
            except PanelError as exc:
                if "404" not in str(exc) and "not found" not in str(exc).lower():
                    raise
        settings_payload = json.dumps({"clients": [client_spec]})
        payload = {"id": inbound_id, "settings": settings_payload}
        return await self._request("POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json_data=payload)

    async def delete_client(
        self,
        inbound_id: int,
        client_uuid: str,
        client_email: Optional[str] = None,
        all_inbound_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Delete client from inbound(s)."""
        if client_email:
            try:
                safe_email = urllib.parse.quote(client_email)
                return await self._request("POST", f"/panel/api/clients/del/{safe_email}")
            except Exception:
                pass
        target_inbounds = list(dict.fromkeys(([inbound_id] if inbound_id else []) + (all_inbound_ids or [])))
        last_res = None
        for inb_id in (target_inbounds or [inbound_id]):
            try:
                last_res = await self._request("POST", f"/panel/api/inbounds/{inb_id}/delClient/{client_uuid}")
            except Exception:
                try:
                    last_res = await self._request("POST", f"/panel/api/inbounds/delClient/{client_uuid}")
                except Exception:
                    pass
        if last_res is not None:
            return last_res
        return await self._request("POST", f"/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}")

    async def get_client_traffic(self, email: str) -> Optional[Dict[str, Any]]:
        """Retrieve client traffic stats by email. Supports modern 3x-ui and legacy fallback."""
        safe_email = urllib.parse.quote(email)
        try:
            res = await self._request("GET", f"/panel/api/clients/traffic/{safe_email}")
            return res.get("obj")
        except PanelError as exc:
            if "404" in str(exc):
                try:
                    res = await self._request("GET", f"/panel/api/inbounds/getClientTraffics/{safe_email}")
                    return res.get("obj")
                except PanelError as legacy_err:
                    logger.warning(f"Failed to fetch traffic (legacy) for {email}: {legacy_err}")
                    return None
            logger.warning(f"Failed to fetch traffic for {email}: {exc}")
            return None

    async def reset_client_traffic(self, inbound_id: int, email: str) -> Dict[str, Any]:
        """Reset traffic counters for a specific client email."""
        safe_email = urllib.parse.quote(email)
        try:
            return await self._request("POST", f"/panel/api/clients/resetTraffic/{safe_email}")
        except PanelError as exc:
            if "404" in str(exc):
                try:
                    return await self._request("POST", f"/panel/api/inbounds/{inbound_id}/resetClientTraffic/{safe_email}")
                except PanelError as inb_err:
                    if "404" in str(inb_err):
                        return await self._request("POST", f"/panel/api/inbounds/resetClientTraffic/{safe_email}")
                    raise
            raise

    async def get_online_clients(self) -> List[str]:
        """Fetch list of online client emails from panel."""
        try:
            res = await self._request("POST", "/panel/api/inbounds/onlines")
            obj = res.get("obj")
            if isinstance(obj, list):
                return [str(x) for x in obj if x]
        except Exception:
            try:
                res = await self._request("GET", "/panel/api/inbounds/onlines")
                obj = res.get("obj")
                if isinstance(obj, list):
                    return [str(x) for x in obj if x]
            except Exception as exc:
                logger.debug(f"Failed to fetch online clients: {exc}")
        return []

