"""High-level panel adapter and config link generation."""
import base64
import json
import logging
from typing import Any, Dict, List, Optional
import urllib.parse
from app.integrations.xui.client import ThreeXUIClient
from app.integrations.xui.schemas import (
    ClientSpec,
    InboundData,
    PanelServerStatus,
    ClientTrafficStats,
)
from app.core.exceptions import PanelError

logger = logging.getLogger(__name__)


class ThreeXUIPanelAdapter:
    """Adapter decoupling domain services from raw 3x-ui endpoints."""

    def __init__(self, client: ThreeXUIClient):
        self.client = client

    async def test_connection(self) -> bool:
        try:
            return await self.client.login()
        except Exception as e:
            logger.error(f"Panel test_connection failed: {e}")
            return False

    async def get_server_status(self) -> PanelServerStatus:
        raw = await self.client.get_status()
        obj = raw.get("obj") if isinstance(raw, dict) else {}
        if not isinstance(obj, dict):
            obj = {}
        cpu = obj.get("cpu", 0.0)
        mem = obj.get("mem") if isinstance(obj.get("mem"), dict) else {}
        disk = obj.get("disk") if isinstance(obj.get("disk"), dict) else {}
        uptime = obj.get("uptime", 0)
        xray = obj.get("xray") if isinstance(obj.get("xray"), dict) else {}

        def _safe_int(v: Any, default: int = 0) -> int:
            if v is None:
                return default
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        return PanelServerStatus(
            cpu=float(cpu) if isinstance(cpu, (int, float)) else 0.0,
            mem_current=_safe_int(mem.get("current")),
            mem_total=_safe_int(mem.get("total")),
            disk_current=_safe_int(disk.get("current")),
            disk_total=_safe_int(disk.get("total")),
            uptime=_safe_int(uptime),
            xray_state=str(xray.get("state") or "unknown"),
            xray_version=str(xray.get("version") or "unknown"),
            raw=obj,
        )

    async def list_inbounds(self) -> List[InboundData]:
        raw_list = await self.client.get_inbounds()
        if not isinstance(raw_list, list):
            return []
        inbounds: List[InboundData] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            settings_dict = {}
            raw_settings = item.get("settings")
            if isinstance(raw_settings, str):
                try:
                    parsed = json.loads(raw_settings)
                    if isinstance(parsed, dict):
                        settings_dict = parsed
                except Exception:
                    pass
            elif isinstance(raw_settings, dict):
                settings_dict = raw_settings

            stream_dict = {}
            raw_stream = item.get("streamSettings")
            if isinstance(raw_stream, str):
                try:
                    parsed = json.loads(raw_stream)
                    if isinstance(parsed, dict):
                        stream_dict = parsed
                except Exception:
                    pass
            elif isinstance(raw_stream, dict):
                stream_dict = raw_stream

            inb_id = item.get("id")
            if inb_id is None:
                continue
            try:
                inb_id = int(inb_id)
            except (ValueError, TypeError):
                continue

            inbound_obj = InboundData(
                id=inb_id,
                up=int(item.get("up") or 0) if isinstance(item.get("up"), (int, float, str)) else 0,
                down=int(item.get("down") or 0) if isinstance(item.get("down"), (int, float, str)) else 0,
                total=int(item.get("total") or 0) if isinstance(item.get("total"), (int, float, str)) else 0,
                remark=str(item.get("remark") or ""),
                enable=bool(item.get("enable", True)),
                port=int(item.get("port") or 0) if isinstance(item.get("port"), (int, float, str)) else 0,
                protocol=str(item.get("protocol") or "vless"),
                settings=settings_dict,
                stream_settings=stream_dict,
                tag=str(item.get("tag") or ""),
                listen=str(item.get("listen") or "0.0.0.0"),
            )
            inbounds.append(inbound_obj)
        return inbounds

    async def add_client(self, inbound_id: int, spec: ClientSpec) -> Dict[str, Any]:
        """Add new client to panel inbound."""
        payload = spec.model_dump()
        return await self.client.add_client(inbound_id, payload)

    async def add_clients_to_inbounds(self, inbound_ids: List[int], spec: Any) -> Dict[str, Any]:
        """Add new client across multiple panel inbounds simultaneously."""
        if not inbound_ids:
            return {"success": True, "msg": "No inbounds provided"}
        if hasattr(spec, "model_dump"):
            payload = spec.model_dump()
        elif isinstance(spec, dict):
            payload = spec
        else:
            payload = dict(spec)

        if len(inbound_ids) == 1:
            return await self.add_client(inbound_ids[0], spec if isinstance(spec, ClientSpec) else ClientSpec(**payload))
        return await self.client.add_clients_to_inbounds(inbound_ids, payload)

    async def update_client(self, inbound_id: int, client_uuid: str, spec: ClientSpec) -> bool:
        """Update existing client settings on panel."""
        payload = spec.model_dump()
        res = await self.client.update_client(inbound_id, client_uuid, payload)
        return res.get("success", False)

    async def delete_client(
        self,
        inbound_id: int,
        client_uuid: str,
        client_email: Optional[str] = None,
        all_inbound_ids: Optional[List[int]] = None,
    ) -> bool:
        """Remove client from panel."""
        res = await self.client.delete_client(
            inbound_id,
            client_uuid,
            client_email=client_email,
            all_inbound_ids=all_inbound_ids,
        )
        return res.get("success", False)

    async def get_client_traffic(self, email: str) -> Optional[ClientTrafficStats]:
        """Fetch client traffic data, handling both single-dict and multi-inbound list responses."""
        obj = await self.client.get_client_traffic(email)
        if not obj:
            return None

        def _safe_int(v: Any, default: int = 0) -> int:
            if v is None:
                return default
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        if isinstance(obj, list):
            valid_items = [item for item in obj if isinstance(item, dict)]
            if not valid_items:
                return None
            up_sum = sum(_safe_int(item.get("up")) for item in valid_items)
            down_sum = sum(_safe_int(item.get("down")) for item in valid_items)
            total_quota = max((_safe_int(item.get("total")) for item in valid_items), default=0)
            max_expiry = max((_safe_int(item.get("expiryTime")) for item in valid_items), default=0)
            is_enabled = all(bool(item.get("enable", True)) for item in valid_items)
            return ClientTrafficStats(
                email=email,
                up=up_sum,
                down=down_sum,
                total=total_quota,
                expiry_time=max_expiry,
                enable=is_enabled,
            )
        elif isinstance(obj, dict):
            return ClientTrafficStats(
                email=str(obj.get("email") or email),
                up=_safe_int(obj.get("up")),
                down=_safe_int(obj.get("down")),
                total=_safe_int(obj.get("total")),
                expiry_time=_safe_int(obj.get("expiryTime")),
                enable=bool(obj.get("enable", True)),
            )
        return None

    async def reset_client_traffic(self, inbound_id: int, email: str) -> bool:
        res = await self.client.reset_client_traffic(inbound_id, email)
        return res.get("success", False)

    async def get_online_clients(self) -> List[str]:
        """Retrieve list of active online client emails."""
        return await self.client.get_online_clients()


    async def get_subscription_url(
        self,
        server_host: str,
        sub_port: int,
        sub_id: str,
        use_ssl: bool = True,
    ) -> str:
        """
        Dynamically retrieve subscription URL template directly from panel settings (subURI / subDomain)
        or construct standardized fallback.
        """
        try:
            settings = await self.client.get_default_settings()
            sub_uri = (settings.get("subURI") or "").strip()
            if sub_uri:
                return f"{sub_uri.rstrip('/')}/{sub_id}"

            sub_domain = (settings.get("subDomain") or "").strip()
            if sub_domain:
                port_part = f":{sub_port}" if sub_port not in (80, 443) else ""
                return f"https://{sub_domain}{port_part}/sub/{sub_id}"
        except Exception as e:
            logger.debug(f"Could not retrieve dynamic subURI from panel: {e}")

        scheme = "https" if use_ssl else "http"
        port_part = f":{sub_port}" if sub_port not in (80, 443) else ""
        return f"{scheme}://{server_host}{port_part}/sub/{sub_id}"

    def generate_subscription_url(
        self,
        server_host: str,
        sub_port: int,
        sub_id: str,
        use_ssl: bool = False,
    ) -> str:
        """Produce the standardized 3x-ui client subscription link."""
        scheme = "https" if use_ssl else "http"
        return f"{scheme}://{server_host}:{sub_port}/sub/{sub_id}"

    def generate_client_url(
        self,
        protocol: str,
        inbound_data: Dict[str, Any],
        client_data: Dict[str, Any],
        server_host: str,
    ) -> str:
        """Generate direct protocol links (vless://, vmess://, trojan://, ss://)."""
        proto = str(protocol or "").lower()
        if not isinstance(inbound_data, dict):
            inbound_data = {}
        if not isinstance(client_data, dict):
            client_data = {}

        port = inbound_data.get("port", 443)
        remark = urllib.parse.quote(str(client_data.get("remark") or inbound_data.get("remark") or "VPN-Private"))
        raw_stream = inbound_data.get("stream_settings")
        stream = raw_stream if isinstance(raw_stream, dict) else {}
        network = str(stream.get("network") or "tcp")
        security = str(stream.get("security") or "none")
        sni = ""
        path = ""
        host_header = ""

        # Extract TLS/Reality SNI
        fp = ""
        if security == "tls":
            tls_settings = stream.get("tlsSettings")
            tls_dict = tls_settings if isinstance(tls_settings, dict) else {}
            sni = str(tls_dict.get("serverName") or server_host)
        elif security == "reality":
            reality_settings = stream.get("realitySettings")
            reality_dict = reality_settings if isinstance(reality_settings, dict) else {}
            server_names = reality_dict.get("serverNames")
            sni = str(server_names[0] if isinstance(server_names, list) and server_names else server_host)
            pbk = str(reality_dict.get("publicKey") or "")
            short_ids = reality_dict.get("shortIds")
            sid = str(reality_dict.get("shortId") or (short_ids[0] if isinstance(short_ids, list) and short_ids else ""))
            fp = str(reality_dict.get("fingerprint") or client_data.get("fp") or "chrome")

        # Extract WS/gRPC path
        if network == "ws":
            ws_settings = stream.get("wsSettings")
            ws_dict = ws_settings if isinstance(ws_settings, dict) else {}
            path = urllib.parse.quote(str(ws_dict.get("path") or "/"))
            headers = ws_dict.get("headers")
            host_header = str(headers.get("Host", "") if isinstance(headers, dict) else "")
        elif network == "grpc":
            grpc_settings = stream.get("grpcSettings")
            grpc_dict = grpc_settings if isinstance(grpc_settings, dict) else {}
            path = urllib.parse.quote(str(grpc_dict.get("serviceName") or ""))

        client_id = str(client_data.get("id") or client_data.get("client_uuid") or client_data.get("password") or "")

        if proto == "vless":
            flow = client_data.get("flow", "")
            params = [
                f"type={network}",
                f"security={security}",
            ]
            if path:
                params.append(f"path={path}")
            if network == "grpc" and path:
                params.append(f"serviceName={path}")
            if sni:
                params.append(f"sni={sni}")
            if host_header:
                params.append(f"host={host_header}")
            if flow:
                params.append(f"flow={flow}")
            if security == "reality":
                if pbk:
                    params.append(f"pbk={pbk}")
                if sid:
                    params.append(f"sid={sid}")
                if fp:
                    params.append(f"fp={fp}")

            query_str = "&".join(params)
            return f"vless://{client_id}@{server_host}:{port}?{query_str}#{remark}"

        elif proto == "vmess":
            vmess_payload = {
                "v": "2",
                "ps": remark,
                "add": server_host,
                "port": port,
                "id": client_id,
                "aid": 0,
                "net": network,
                "type": "none",
                "host": host_header or sni,
                "path": path,
                "tls": security if security in ("tls",) else "",
                "sni": sni,
            }
            raw_json = json.dumps(vmess_payload)
            b64_str = base64.b64encode(raw_json.encode()).decode()
            return f"vmess://{b64_str}"

        elif proto == "trojan":
            password = client_data.get("password") or client_id
            params = [f"security={security}"]
            if sni:
                params.append(f"sni={sni}")
            if network != "tcp":
                params.append(f"type={network}")
            if path:
                params.append(f"path={path}")
            if network == "grpc" and path:
                params.append(f"serviceName={path}")
            query_str = "&".join(params)
            return f"trojan://{password}@{server_host}:{port}?{query_str}#{remark}"

        elif proto == "shadowsocks":
            raw_inb_settings = inbound_data.get("settings")
            inb_settings = raw_inb_settings if isinstance(raw_inb_settings, dict) else {}
            cipher = client_data.get("method") or inb_settings.get("method", "aes-256-gcm")
            password = client_data.get("password") or client_id or inb_settings.get("password", "")
            auth_str = f"{cipher}:{password}"
            b64_auth = base64.b64encode(auth_str.encode()).decode()
            return f"ss://{b64_auth}@{server_host}:{port}#{remark}"

        return f"vpn://{client_id}@{server_host}:{port}"
