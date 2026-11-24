"""Centralized Server Allocation Engine."""
import json
import logging
import re
import uuid
import urllib.parse
from typing import Optional, Tuple, List, Dict, Any, Set
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models.panel import Panel, Inbound, Server
from app.database.models.product import Product
from app.core.exceptions import ProvisioningError

logger = logging.getLogger(__name__)


LOCATION_TO_INBOUND_ID = {
    "DE-1": [1, 29],
    "DE-2": [29, 1],
    "DE": [1, 29],
    "FI": [2],
    "SE-1": [1, 2],
    "SE-2": [1, 2],
    "SE-3": [1, 2],
    "SE": [1, 2],
    "NL-1": [20],
    "NL-2": [21],
    "NL": [20, 21],
    "UK-1": [11],
    "UK-2": [12, 11],
    "UK-3": [14, 11],
    "UK-4": [13, 11],
    "UK-5": [15, 11],
    "UK": [11, 12, 14, 13, 15],
    "GB": [11, 12, 14, 13, 15],
    "FR-1": [9],
    "FR-2": [10],
    "FR-3": [9, 10],
    "FR": [9, 10],
    "US-1": [23],
    "US-2": [26, 23],
    "US-3": [25, 23],
    "US-4": [24, 23],
    "US": [23, 26, 25, 24],
    "AZ": [4],
    "AM": [5],
    "DK-1": [6],
    "DK-2": [7],
    "DK": [6, 7],
    "EG": [8],
    "GL": [16],
    "IT-1": [17],
    "IT-2": [19],
    "IT": [17, 19],
    "GH": [18],
    "PL": [22],
    "AR": [27],
    "NO": [28],
    "AE": [1],
    "CY": [4],
    "NODE": [30],
}

COUNTRY_METADATA = {
    "DE": {"fa": "آلمان", "flag": "🇩🇪", "aliases": ["GERMANY", "FRANKFURT", "DEUTSCHLAND"]},
    "FR": {"fa": "فرانسه", "flag": "🇫🇷", "aliases": ["FRANCE", "PARIS"]},
    "UK": {"fa": "انگلستان", "flag": "🇬🇧", "aliases": ["UNITED KINGDOM", "UK", "MANCHESTER", "SOUTHAMPTON", "TOTTENHAM", "LONDON", "ENGLAND", "BRITAIN"]},
    "GB": {"fa": "انگلستان", "flag": "🇬🇧", "aliases": ["UNITED KINGDOM", "UK", "MANCHESTER", "SOUTHAMPTON", "TOTTENHAM", "LONDON", "ENGLAND", "BRITAIN"]},
    "US": {"fa": "آمریکا", "flag": "🇺🇸", "aliases": ["USA", "UNITED STATES", "AMERICA", "WILMINGTON", "BROOKLYN", "NEW YORK", "OHIO", "LOS ANGELES", "CHICAGO", "DALLAS", "MIAMI", "SEATTLE"]},
    "NL": {"fa": "هلند", "flag": "🇳🇱", "aliases": ["NETHERLANDS", "AMSTERDAM", "HOLLAND"]},
    "TR": {"fa": "ترکیه", "flag": "🇹🇷", "aliases": ["TURKEY", "ISTANBUL", "ANKARA", "TURKIYE"]},
    "FI": {"fa": "فنلاند", "flag": "🇫🇮", "aliases": ["FINLAND", "HELSINKI", "WARP", "CLOUDFLARE"]},
    "SE": {"fa": "سوئد", "flag": "🇸🇪", "aliases": ["SWEDEN", "STOCKHOLM"]},
    "IT": {"fa": "ایتالیا", "flag": "🇮🇹", "aliases": ["ITALY", "MILAN", "ROME"]},
    "AZ": {"fa": "آذربایجان", "flag": "🇦🇿", "aliases": ["AZERBAIJAN", "BAKU"]},
    "AM": {"fa": "ارمنستان", "flag": "🇦🇲", "aliases": ["ARMENIA", "YEREVAN"]},
    "DK": {"fa": "دانمارک", "flag": "🇩🇰", "aliases": ["DENMARK", "COPENHAGEN"]},
    "NO": {"fa": "نروژ", "flag": "🇳🇴", "aliases": ["NORWAY", "OSLO"]},
    "PL": {"fa": "لهستان", "flag": "🇵🇱", "aliases": ["POLAND", "WARSAW"]},
    "AR": {"fa": "آرژانتین", "flag": "🇦🇷", "aliases": ["ARGENTINA", "BUENOS AIRES"]},
    "AE": {"fa": "امارات", "flag": "🇦🇪", "aliases": ["DUBAI", "EMIRATES", "UAE", "ABU DHABI"]},
    "CY": {"fa": "قبرس", "flag": "🇨🇾", "aliases": ["CYPRUS", "NICOSIA"]},
    "IR": {"fa": "ایران", "flag": "🇮🇷", "aliases": ["IRAN", "TEHRAN"]},
    "EG": {"fa": "مصر", "flag": "🇪🇬", "aliases": ["EGYPT", "CAIRO"]},
    "GL": {"fa": "گرینلند", "flag": "🇬🇱", "aliases": ["GREENLAND"]},
    "GH": {"fa": "غنا", "flag": "🇬🇭", "aliases": ["GHANA"]},
    "CH": {"fa": "سوئیس", "flag": "🇨🇭", "aliases": ["SWITZERLAND", "ZURICH", "GENEVA"]},
    "ES": {"fa": "اسپانیا", "flag": "🇪🇸", "aliases": ["SPAIN", "MADRID", "BARCELONA"]},
    "CA": {"fa": "کانادا", "flag": "🇨🇦", "aliases": ["CANADA", "TORONTO", "MONTREAL", "VANCOUVER"]},
    "SG": {"fa": "سنگاپور", "flag": "🇸🇬", "aliases": ["SINGAPORE"]},
    "JP": {"fa": "ژاپن", "flag": "🇯🇵", "aliases": ["JAPAN", "TOKYO"]},
    "RU": {"fa": "روسیه", "flag": "🇷🇺", "aliases": ["RUSSIA", "MOSCOW"]},
    "AU": {"fa": "استرالیا", "flag": "🇦🇺", "aliases": ["AUSTRALIA", "SYDNEY", "MELBOURNE"]},
    "AT": {"fa": "اتریش", "flag": "🇦🇹", "aliases": ["AUSTRIA", "VIENNA"]},
    "BE": {"fa": "بلژیک", "flag": "🇧🇪", "aliases": ["BELGIUM", "BRUSSELS"]},
    "RO": {"fa": "رومانی", "flag": "🇷🇴", "aliases": ["ROMANIA", "BUCHAREST"]},
    "UA": {"fa": "اوکراین", "flag": "🇺🇦", "aliases": ["UKRAINE", "KYIV"]},
    "BR": {"fa": "برزیل", "flag": "🇧🇷", "aliases": ["BRAZIL", "SAO PAULO"]},
    "IN": {"fa": "هند", "flag": "🇮🇳", "aliases": ["INDIA", "MUMBAI", "DELHI"]},
    "KR": {"fa": "کره جنوبی", "flag": "🇰🇷", "aliases": ["KOREA", "SEOUL"]},
}

COUNTRY_NAME_MAP = {code: meta["aliases"] for code, meta in COUNTRY_METADATA.items()}


def extract_country_and_subindex(
    remark: Optional[str],
    tag: Optional[str],
    panel_name: str = "",
    remote_inbound_id: Optional[int] = None,
) -> Tuple[str, Optional[int]]:
    """Extract 2-letter country code and optional sub-index from inbound remark, tag, or panel name."""
    # 3X-UI auto-generated tags/remarks (e.g. "inbound-18862", "in-36477-udp") carry no
    # country information; "IN" there is NOT India. Ignore them for country detection.
    auto_tag_re = re.compile(r"^\s*in(?:bound)?[-_]\d+(?:[-_][a-z0-9]+)*\s*$", re.IGNORECASE)
    parts = [p for p in (remark, tag) if p and not auto_tag_re.match(p)]
    text = " ".join(parts).strip()
    upper_text = text.upper()

    # 0. Match by flag emoji (most reliable signal, e.g. "🇩🇪 Germany | Main")
    for code, meta in COUNTRY_METADATA.items():
        if meta["flag"] and meta["flag"] in text:
            if code == "GB":
                code = "UK"
            m = re.search(r"(?<![\d.])0*(\d{1,2})(?![\d.])", text)
            return code, int(m.group(1)) if m else None

    # 1. Match by Persian country name
    for code, meta in COUNTRY_METADATA.items():
        if meta["fa"] in text:
            m = re.search(r"(?<![\d.])0*(\d{1,2})(?![\d.])", text)
            sub = int(m.group(1)) if m else None
            return code, sub

    # 2. Match country code with optional number pattern (e.g. FR-1, FR-01, DEC-FR-02, DE_2, DE-2)
    all_codes = sorted(list(COUNTRY_METADATA.keys()), key=lambda x: -len(x))
    code_pattern = rf"(?:^|[\s\-_/(\[])(" + "|".join(all_codes) + rf")(?:[-_\s]*0*(\d+))?(?:[\s\-_/)\]]|$)"
    m = re.search(code_pattern, upper_text)
    if m:
        c = m.group(1)
        sub = int(m.group(2)) if m.group(2) else None
        return c, sub

    # 3. Match English country names & aliases
    for code, meta in COUNTRY_METADATA.items():
        for alias in meta["aliases"]:
            if re.search(rf"(?<![A-Z]){re.escape(alias)}(?![A-Z])", upper_text):
                m = re.search(r"[-_\s]0*(\d+)", upper_text)
                sub = int(m.group(1)) if m else None
                return code, sub

    # 4. Check Finland WARP
    if "WARP" in upper_text:
        m = re.search(r"(\d+)", upper_text)
        return "FI", int(m.group(1)) if m else None

    # 5. Fallback to Panel Name
    p_upper = (panel_name or "").upper()
    for code, meta in COUNTRY_METADATA.items():
        if meta["fa"] in panel_name or any(
            re.search(rf"(?<![A-Z]){re.escape(a)}(?![A-Z])", p_upper) for a in meta["aliases"]
        ):
            return code, None
    pm = re.search(r"(?:^|[^A-Z])(" + "|".join(sorted(COUNTRY_METADATA, key=len, reverse=True)) + r")(?:[^A-Z]|$)", p_upper)
    if pm:
        return pm.group(1), None

    # 6. Dynamic unclassified location
    return "LOC", None


def detect_protocol_display(
    protocol: Optional[str],
    remark: Optional[str],
    tag: Optional[str],
    stream_settings_raw: Any,
) -> str:
    """Detect human-friendly protocol/transport display name (e.g. XHTTP, Paqet, Reality, WS)."""
    full_text = f"{remark or ''} {tag or ''}".upper()
    settings_dict = {}
    if isinstance(stream_settings_raw, dict):
        settings_dict = stream_settings_raw
    elif isinstance(stream_settings_raw, str) and stream_settings_raw.strip():
        try:
            settings_dict = json.loads(stream_settings_raw)
        except Exception:
            settings_dict = {}

    network = str(settings_dict.get("network", "")).lower()
    security = str(settings_dict.get("security", "")).lower()

    # 1. Paqet
    if "PAQET" in full_text or "PACKET" in full_text:
        return "Paqet"

    # 2. XHTTP / SplitHTTP
    if "XHTTP" in full_text or "SPLITHTTP" in full_text or network in ("xhttp", "splithttp"):
        return "XHTTP"

    # 3. Reality
    if "REALITY" in full_text or security == "reality" or "realitySettings" in settings_dict:
        return "Reality"

    # 4. WebSocket / WS
    if "WS" in full_text or "WEBSOCKET" in full_text or network in ("ws", "websocket"):
        return "WS"

    # 5. gRPC
    if "GRPC" in full_text or network == "grpc":
        return "gRPC"

    # 6. TCP
    if "TCP" in full_text or network == "tcp":
        return "TCP"

    # 7. TLS
    if security == "tls":
        return "TLS"

    proto_clean = (protocol or "").strip().lower()
    if proto_clean == "vless":
        return "VLESS"
    elif proto_clean == "vmess":
        return "VMess"
    elif proto_clean == "trojan":
        return "Trojan"
    elif proto_clean in ("shadowsocks", "ss"):
        return "Shadowsocks"

    return proto_clean.upper() if proto_clean else "VLESS"


def extract_host_from_stream_settings(stream_settings_raw: Any, fallback_host: str) -> str:
    """Extract host/SNI from streamSettings JSON or fallback to panel host."""
    settings_dict = {}
    if isinstance(stream_settings_raw, dict):
        settings_dict = stream_settings_raw
    elif isinstance(stream_settings_raw, str) and stream_settings_raw.strip():
        try:
            settings_dict = json.loads(stream_settings_raw)
        except Exception:
            settings_dict = {}

    candidate = None

    # 1. realitySettings
    reality = settings_dict.get("realitySettings")
    if isinstance(reality, dict):
        server_names = reality.get("serverNames")
        if isinstance(server_names, list) and server_names:
            first_name = str(server_names[0]).strip()
            if first_name:
                candidate = first_name
        if not candidate and reality.get("serverName"):
            candidate = str(reality["serverName"]).strip()
        if not candidate and reality.get("dest"):
            dest = str(reality["dest"]).split(":")[0].strip()
            if dest:
                candidate = dest

    # 2. wsSettings
    if not candidate:
        ws = settings_dict.get("wsSettings")
        if isinstance(ws, dict):
            headers = ws.get("headers")
            if isinstance(headers, dict):
                for k, v in headers.items():
                    if k.lower() == "host" and str(v).strip():
                        candidate = str(v).strip()
                        break

    # 3. tlsSettings
    if not candidate:
        tls = settings_dict.get("tlsSettings")
        if isinstance(tls, dict):
            if tls.get("serverName"):
                candidate = str(tls["serverName"]).strip()

    # 4. tcpSettings
    if not candidate:
        tcp = settings_dict.get("tcpSettings")
        if isinstance(tcp, dict):
            req = tcp.get("header", {}).get("request", {})
            headers = req.get("headers", {}) if isinstance(req, dict) else {}
            for k, v in headers.items():
                if k.lower() == "host":
                    if isinstance(v, list) and v:
                        candidate = str(v[0]).strip()
                    elif isinstance(v, str) and v.strip():
                        candidate = v.strip()
                    break

    # 5. grpcSettings
    if not candidate:
        grpc = settings_dict.get("grpcSettings")
        if isinstance(grpc, dict):
            if grpc.get("authority"):
                candidate = str(grpc["authority"]).strip()
            elif grpc.get("serviceName") and "." in str(grpc.get("serviceName")):
                candidate = str(grpc["serviceName"]).strip()

    # 6. httpSettings
    if not candidate:
        http = settings_dict.get("httpSettings")
        if isinstance(http, dict):
            host = http.get("host")
            if isinstance(host, list) and host:
                candidate = str(host[0]).strip()
            elif isinstance(host, str) and host.strip():
                candidate = host.strip()

    # 7. top-level sni or serverName
    if not candidate:
        if settings_dict.get("sni"):
            candidate = str(settings_dict["sni"]).strip()
        elif settings_dict.get("serverName"):
            candidate = str(settings_dict["serverName"]).strip()

    if candidate and candidate not in ("0.0.0.0", ""):
        return candidate

    return fallback_host


async def sync_inbounds_to_servers(
    db: AsyncSession,
    panel: Panel,
    inbounds: List[Inbound],
) -> List[Server]:
    """
    Synchronize panel Inbound records into corresponding Server entities in the database.
    
    For every Inbound:
    - Parses remark/tag to extract country/location code (FR, DE, etc.).
    - Generates human-friendly name with Persian country name, protocol, and sub-index.
    - Extracts host from streamSettings or falls back to panel hostname.
    - Extracts port from inbound port.
    - Creates or updates corresponding Server entity tied to panel_id with is_active = inbound.is_active.
    """
    if not inbounds:
        return []

    parsed_panel = urllib.parse.urlparse(panel.base_url)
    panel_host = parsed_panel.hostname or panel.base_url
    panel_port = parsed_panel.port or 443

    # Fetch existing servers for this panel
    existing_srvs_res = await db.execute(select(Server).where(Server.panel_id == panel.id))
    existing_servers = list(existing_srvs_res.scalars().all())

    # Map existing servers by inbound_id
    server_by_inbound_id: Dict[uuid.UUID, Server] = {
        s.inbound_id: s for s in existing_servers if s.inbound_id is not None
    }
    unlinked_servers = [s for s in existing_servers if s.inbound_id is None]

    # Pre-calculate assigned sub-indices per country
    country_inbound_tuples = []
    used_indices_per_country: Dict[str, Set[int]] = {}

    for inb in inbounds:
        country, explicit_sub = extract_country_and_subindex(
            inb.remark, inb.tag, panel.name, inb.remote_inbound_id
        )
        if explicit_sub is None:
            # Keep a stable code for already-linked servers of the same country so that
            # products referencing e.g. "DE-2" don't break on every sync.
            prev = server_by_inbound_id.get(inb.id)
            if prev is not None and prev.location_code and "-" in prev.location_code:
                p_c, p_n = prev.location_code.split("-", 1)
                if p_c.upper() == country and p_n.isdigit() and int(p_n) < 1000:
                    explicit_sub = int(p_n)
        if country not in used_indices_per_country:
            used_indices_per_country[country] = set()
        if explicit_sub is not None:
            used_indices_per_country[country].add(explicit_sub)
        country_inbound_tuples.append((inb, country, explicit_sub))

    # Also reserve numbered codes of servers that are NOT part of this sync batch
    batch_ids = {inb.id for inb in inbounds}
    for s in existing_servers:
        if s.inbound_id in batch_ids:
            continue
        if s.location_code and "-" in s.location_code:
            c_part, num_part = s.location_code.split("-", 1)
            c_part = c_part.upper()
            if num_part.isdigit():
                used_indices_per_country.setdefault(c_part, set()).add(int(num_part))

    resolved_inbounds = []
    next_counter_per_country: Dict[str, int] = {}

    for inb, country, explicit_sub in country_inbound_tuples:
        if explicit_sub is not None:
            sub = explicit_sub
        else:
            ctr = next_counter_per_country.get(country, 1)
            while ctr in used_indices_per_country[country]:
                ctr += 1
            used_indices_per_country[country].add(ctr)
            next_counter_per_country[country] = ctr + 1
            sub = ctr

        resolved_inbounds.append((inb, country, sub))

    # All server names currently in DB across all panels to prevent unique constraint conflicts
    all_server_names = set((await db.execute(select(Server.name))).scalars().all())

    synced_servers: List[Server] = []

    for inb, country, sub in resolved_inbounds:
        loc_code = f"{country}-{sub}" if country != "ALL" else f"ALL-{sub}"
        
        meta = COUNTRY_METADATA.get(country, {"fa": panel.name or "عمومی", "flag": "🌐"})
        persian_name = meta["fa"]
        flag_emoji = meta["flag"]

        proto_disp = detect_protocol_display(inb.protocol, inb.remark, inb.tag, inb.stream_settings)
        base_name = f"{flag_emoji} {persian_name} - {proto_disp} ({loc_code})"

        host = extract_host_from_stream_settings(inb.stream_settings, panel_host)
        port = int(inb.port) if (inb.port and inb.port > 0) else panel_port

        # Check if server already exists for this inbound
        srv = server_by_inbound_id.get(inb.id)

        if not srv and unlinked_servers:
            # Check if any unlinked server matches location_code or is a placeholder
            matched_unlinked = None
            for us in unlinked_servers:
                if us.location_code and (us.location_code == loc_code or us.location_code == country):
                    matched_unlinked = us
                    break
            if not matched_unlinked:
                # Prefer an unlinked placeholder with ALL
                placeholders = [us for us in unlinked_servers if us.location_code == "ALL"]
                matched_unlinked = placeholders[0] if placeholders else unlinked_servers[0]

            unlinked_servers.remove(matched_unlinked)
            srv = matched_unlinked
            srv.inbound_id = inb.id

        if srv is not None:
            all_server_names.discard(srv.name)

        # Ensure name uniqueness
        candidate_name = base_name
        counter = 2
        while candidate_name in all_server_names:
            candidate_name = f"{base_name} #{counter}"
            counter += 1
        all_server_names.add(candidate_name)

        if srv:
            srv.name = candidate_name
            srv.location_code = loc_code
            srv.location_name = persian_name
            srv.flag_emoji = flag_emoji
            srv.subscription_host = host
            srv.subscription_port = port
            srv.is_active = inb.is_active
            srv.inbound_id = inb.id
        else:
            srv = Server(
                id=uuid.uuid4(),
                inbound_id=inb.id,
                panel_id=panel.id,
                name=candidate_name,
                location_code=loc_code,
                location_name=persian_name,
                flag_emoji=flag_emoji,
                priority=10,
                subscription_host=host,
                subscription_port=port,
                is_active=inb.is_active,
                capacity_limit=1000,
                current_clients=inb.client_count or 0,
            )
            db.add(srv)
            server_by_inbound_id[inb.id] = srv

        synced_servers.append(srv)

    await db.flush()
    return synced_servers


def inbound_matches_location(remark: str, tag: str, loc_code: str, remote_inbound_id: Optional[int] = None) -> bool:
    """Check if an inbound's remark, tag, or ID corresponds to a target location/country code or specific inbound."""
    import re

    if not loc_code or loc_code.strip().upper() == "ALL":
        return True

    parts = [p.strip().upper() for p in loc_code.split(",") if p.strip()]
    if not parts or "ALL" in parts:
        return True
    if len(parts) > 1:
        return any(inbound_matches_location(remark, tag, p, remote_inbound_id) for p in parts)

    target = parts[0]

    # 1. Direct Inbound ID matching (e.g. INB-10, INB-1, or raw integer string)
    if remote_inbound_id is not None:
        target_inb_str = target[4:] if target.startswith("INB-") else target
        if target_inb_str.isdigit() and int(target_inb_str) == remote_inbound_id:
            return True
        if target in LOCATION_TO_INBOUND_ID and remote_inbound_id in LOCATION_TO_INBOUND_ID[target]:
            return True

    full_text = f"{remark or ''} {tag or ''}".upper()

    # 2. Specific location code with index/port (e.g. DE-2, FR-1, IN-45029)
    if "-" in target:
        if target in full_text or target.replace("-", "_") in full_text or target.replace("-", " ") in full_text:
            return True
        c_part, num_part = target.split("-", 1)
        c_extracted, sub_extracted = extract_country_and_subindex(remark, tag, remote_inbound_id=remote_inbound_id)
        if c_extracted == c_part:
            if num_part.isdigit() and sub_extracted is not None and int(num_part) == sub_extracted:
                return True

    country = target.split("-")[0]

    if remote_inbound_id is not None:
        if country in LOCATION_TO_INBOUND_ID and remote_inbound_id in LOCATION_TO_INBOUND_ID[country]:
            return True

    # 3. Match country name or city alias
    aliases = COUNTRY_NAME_MAP.get(country, [])
    if any(a in full_text for a in aliases):
        return True

    # 4. Finland is routed via WARP outbound
    if country == "FI" and "WARP" in full_text:
        return True

    # 5. Germany special cases
    if country == "DE" and (
        full_text.strip() == "DEC"
        or "DEC-DE" in full_text
        or "REALITY-DE" in full_text
        or "WS-DE" in full_text
        or "-DE-" in full_text
        or full_text.endswith("-DE")
        or "GERMANY" in full_text
    ):
        return True

    # 6. Match delimited country code: e.g. -DK-, -DK-02, _DK_, [space]DK[space]
    pattern = rf"(?:^|[\s\-_]){re.escape(country)}(?:[\s\-_]|$|\d+)"
    return bool(re.search(pattern, full_text))


class ServerAllocationEngine:
    """Selects optimal server and inbound based on health, capacity, priority, and location."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def allocate_server_and_inbound(
        self,
        location_code: Optional[str],
        protocol: str,
        product: Optional[Product] = None,
        preferred_server_id: Optional[str] = None,
    ) -> Tuple[Server, Inbound, Panel]:
        """
        Evaluate candidate servers and return the best (Server, Inbound, Panel).
        Raises ProvisioningError if no eligible server/inbound is found.
        """
        base_query = (
            select(Server, Panel)
            .join(Panel, Server.panel_id == Panel.id)
            .where(
                and_(
                    Server.is_active.is_(True),
                    Panel.is_active.is_(True),
                    Panel.is_healthy.is_(True),
                    Server.current_clients < Server.capacity_limit,
                )
            )
        )

        results = []
        loc_clean = (location_code or "").strip().upper()
        is_all = (not loc_clean) or (loc_clean == "ALL")

        # 1. If preferred server is specified, check it first
        if preferred_server_id:
            pref_uuid = None
            if isinstance(preferred_server_id, uuid.UUID):
                pref_uuid = preferred_server_id
            elif isinstance(preferred_server_id, str):
                try:
                    pref_uuid = uuid.UUID(preferred_server_id)
                except (ValueError, TypeError):
                    pref_uuid = None
            if pref_uuid is not None:
                pref_query = base_query.where(Server.id == pref_uuid)
                pref_results = (await self.session.execute(pref_query)).all()
                if pref_results:
                    results = pref_results
                    pref_srv = pref_results[0][0]
                    if is_all and pref_srv.location_code:
                        loc_clean = pref_srv.location_code
                        is_all = False
                else:
                    logger.warning(
                        f"Preferred server {preferred_server_id} is inactive or panel offline. Falling back."
                    )

        # 2. If specific location(s) requested (and not "ALL")
        if not results and not is_all:
            raw_parts = [p.strip().upper() for p in loc_clean.split(",") if p.strip()]
            if "ALL" in raw_parts:
                is_all = True
            else:
                loc_clauses = []
                for target in raw_parts:
                    base_c = target.split("-")[0]
                    loc_clauses.extend([
                        Server.location_code == target,
                        Server.location_code.like(f"{target}-%"),
                        Server.location_code.like(f"{base_c}-%"),
                        Server.location_code == base_c,
                    ])
                loc_query = base_query.where(or_(*loc_clauses)).order_by(
                    Server.priority.desc(), Server.current_clients.asc()
                )
                results = (await self.session.execute(loc_query)).all()
                if not results and len(raw_parts) == 1:
                    msg = f"هیچ سرور فعالی با لوکیشن {location_code} و ظرفیت خالی یافت نشد."
                    logger.error(msg)
                    raise ProvisioningError(msg)

        # 3. For "ALL" or multi-location fallback: query all active servers on healthy panels
        if not results:
            gen_query = base_query.order_by(Server.priority.desc(), Server.current_clients.asc())
            results = (await self.session.execute(gen_query)).all()

        # 4. If STILL no servers found for "ALL", check if there are active & healthy panels without active server entities
        if not results:
            healthy_panels = (
                await self.session.execute(
                    select(Panel).where(Panel.is_active.is_(True), Panel.is_healthy.is_(True))
                )
            ).scalars().all()

            for hp in healthy_panels:
                srv_res = await self.session.execute(
                    select(Server).where(Server.panel_id == hp.id)
                )
                existing_srvs = srv_res.scalars().all()
                if existing_srvs:
                    for srv in existing_srvs:
                        srv.is_active = True
                    await self.session.flush()
                else:
                    parsed = urllib.parse.urlparse(hp.base_url)
                    new_srv = Server(
                        id=uuid.uuid4(),
                        name=f"سرور {hp.name}",
                        location_code="ALL",
                        location_name=hp.name,
                        flag_emoji="🌐",
                        priority=10,
                        panel_id=hp.id,
                        subscription_host=parsed.hostname or hp.base_url,
                        subscription_port=parsed.port or 443,
                        is_active=True,
                        capacity_limit=1000,
                    )
                    self.session.add(new_srv)
                    await self.session.flush()

            # Re-run general query
            gen_query = base_query.order_by(Server.priority.desc(), Server.current_clients.asc())
            results = (await self.session.execute(gen_query)).all()

        if not results:
            msg = f"هیچ سرور فعالی با لوکیشن {location_code} و ظرفیت خالی یافت نشد."
            logger.error(msg)
            raise ProvisioningError(msg)

        # Look for eligible inbound on candidate servers' panels
        target_proto = (protocol or "vless").lower()
        for server, panel in results:
            if getattr(server, "inbound_id", None):
                direct_inb = await self.session.get(Inbound, server.inbound_id)
                if (
                    direct_inb
                    and direct_inb.is_active
                    and direct_inb.client_count < direct_inb.max_clients
                    and (not target_proto or direct_inb.protocol.lower() == target_proto)
                ):
                    logger.info(
                        f"Allocated Server via direct inbound_id: {server.name} ({server.location_code}), "
                        f"Panel: {panel.name}, Inbound: #{direct_inb.remote_inbound_id} ({direct_inb.remark})"
                    )
                    return server, direct_inb, panel

            inbound_query = (
                select(Inbound)
                .where(
                    and_(
                        Inbound.panel_id == panel.id,
                        Inbound.protocol == target_proto,
                        Inbound.is_active.is_(True),
                        Inbound.client_count < Inbound.max_clients,
                    )
                )
                .order_by(Inbound.client_count.asc())
            )
            inbounds = (await self.session.execute(inbound_query)).scalars().all()

            # Target location: if specific location is requested and not ALL, use it; otherwise match server's own location
            loc_to_match = loc_clean if not is_all else server.location_code
            if inbounds and loc_to_match and loc_to_match.upper() != "ALL":
                matching_inb = [
                    ib for ib in inbounds
                    if inbound_matches_location(ib.remark, ib.tag, loc_to_match, ib.remote_inbound_id)
                ]
                if matching_inb:
                    inbound = matching_inb[0]
                    logger.info(
                        f"Allocated Server: {server.name} ({server.location_code}), "
                        f"Panel: {panel.name}, Matched Inbound: #{inbound.remote_inbound_id} ({inbound.remark})"
                    )
                    return server, inbound, panel

        # Fallback if no server had a direct location-tag match: pick first available inbound from results
        for server, panel in results:
            fallback_inb_query = (
                select(Inbound)
                .where(
                    and_(
                        Inbound.panel_id == panel.id,
                        Inbound.protocol == target_proto,
                        Inbound.is_active.is_(True),
                        Inbound.client_count < Inbound.max_clients,
                    )
                )
                .order_by(Inbound.client_count.asc())
            )
            fallback_inb = (await self.session.execute(fallback_inb_query)).scalars().first()
            if fallback_inb:
                logger.info(
                    f"Allocated Server: {server.name} ({server.location_code}), "
                    f"Panel: {panel.name}, Fallback Inbound: #{fallback_inb.remote_inbound_id} ({fallback_inb.protocol})"
                )
                return server, fallback_inb, panel

        raise ProvisioningError(
            f"سرور با لوکیشن {location_code} یافت شد اما اینباند با ظرفیت خالی روی پنل متصل وجود ندارد."
        )

    async def increment_capacity(self, server_id, inbound_id):
        srv_uuid = uuid.UUID(str(server_id)) if server_id and not isinstance(server_id, uuid.UUID) else server_id
        inb_uuid = uuid.UUID(str(inbound_id)) if inbound_id and not isinstance(inbound_id, uuid.UUID) else inbound_id
        if srv_uuid:
            server = await self.session.get(Server, srv_uuid)
            if server:
                server.current_clients += 1
        if inb_uuid:
            inbound = await self.session.get(Inbound, inb_uuid)
            if inbound:
                inbound.client_count += 1
        await self.session.flush()

    async def decrement_capacity(self, server_id, inbound_id):
        srv_uuid = uuid.UUID(str(server_id)) if server_id and not isinstance(server_id, uuid.UUID) else server_id
        inb_uuid = uuid.UUID(str(inbound_id)) if inbound_id and not isinstance(inbound_id, uuid.UUID) else inbound_id
        if srv_uuid:
            server = await self.session.get(Server, srv_uuid)
            if server and server.current_clients > 0:
                server.current_clients -= 1
        if inb_uuid:
            inbound = await self.session.get(Inbound, inb_uuid)
            if inbound and inbound.client_count > 0:
                inbound.client_count -= 1
        await self.session.flush()
