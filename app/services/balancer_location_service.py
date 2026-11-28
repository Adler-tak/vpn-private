"""Service for managing dynamic balancer locations and multi-provider availability."""
import logging
from typing import Dict, List, Optional, Any
from sqlalchemy import select, update
from app.database.session import AsyncSessionLocal
from app.database.models.panel import BalancerLocation

logger = logging.getLogger(__name__)

# Master catalog of supported locations across PIA and Windscribe
MASTER_LOCATION_CATALOG = [
    # --- Supported Balancer Locations Catalog ---
    {
        "balancer_tag": "DE", "country_code": "de", "name_en": "Germany", "name_fa": "آلمان",
        "flag_emoji": "🇩🇪", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 1
    },
    {
        "balancer_tag": "US-BL", "country_code": "us", "name_en": "United States", "name_fa": "آمریکا",
        "flag_emoji": "🇺🇸", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 2
    },
    {
        "balancer_tag": "NL", "country_code": "nl", "name_en": "Netherlands", "name_fa": "هلند",
        "flag_emoji": "🇳🇱", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 3
    },
    {
        "balancer_tag": "FR", "country_code": "fr", "name_en": "France", "name_fa": "فرانسه",
        "flag_emoji": "🇫🇷", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 4
    },
    {
        "balancer_tag": "ITALY", "country_code": "it", "name_en": "Italy", "name_fa": "ایتالیا",
        "flag_emoji": "🇮🇹", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 5
    },
    {
        "balancer_tag": "UK", "country_code": "uk", "name_en": "United Kingdom", "name_fa": "انگلیس",
        "flag_emoji": "🇬🇧", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 6
    },
    {
        "balancer_tag": "NO", "country_code": "no", "name_en": "Norway", "name_fa": "نروژ",
        "flag_emoji": "🇳🇴", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 7
    },
    {
        "balancer_tag": "PL", "country_code": "pl", "name_en": "Poland", "name_fa": "لهستان",
        "flag_emoji": "🇵🇱", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 8
    },
    {
        "balancer_tag": "DK", "country_code": "dk", "name_en": "Denmark", "name_fa": "دانمارک",
        "flag_emoji": "🇩🇰", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 9
    },
    {
        "balancer_tag": "SE", "country_code": "se", "name_en": "Sweden", "name_fa": "سوئد",
        "flag_emoji": "🇸🇪", "is_active": False, "is_supported_pia": False, "is_supported_wd": True,
        "active_provider": "windscribe", "sort_order": 10
    },
    {
        "balancer_tag": "TR", "country_code": "tr", "name_en": "Turkey", "name_fa": "ترکیه",
        "flag_emoji": "🇹🇷", "is_active": False, "is_supported_pia": False, "is_supported_wd": True,
        "active_provider": "windscribe", "sort_order": 11
    },
    {
        "balancer_tag": "CA", "country_code": "ca", "name_en": "Canada", "name_fa": "کانادا",
        "flag_emoji": "🇨🇦", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 12
    },
    {
        "balancer_tag": "CH", "country_code": "ch", "name_en": "Switzerland", "name_fa": "سوئیس",
        "flag_emoji": "🇨🇭", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "windscribe", "sort_order": 13
    },
    {
        "balancer_tag": "ES", "country_code": "es", "name_en": "Spain", "name_fa": "اسپانیا",
        "flag_emoji": "🇪🇸", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "windscribe", "sort_order": 14
    },
    {
        "balancer_tag": "RO", "country_code": "ro", "name_en": "Romania", "name_fa": "رومانی",
        "flag_emoji": "🇷🇴", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "windscribe", "sort_order": 15
    },
    {
        "balancer_tag": "az", "country_code": "cy", "name_en": "Cyprus", "name_fa": "قبرس",
        "flag_emoji": "🇨🇾", "is_active": False, "is_supported_pia": True, "is_supported_wd": False,
        "active_provider": "pia", "sort_order": 16
    },
    {
        "balancer_tag": "YRM", "country_code": "am", "name_en": "Armenia", "name_fa": "ارمنستان",
        "flag_emoji": "🇦🇲", "is_active": False, "is_supported_pia": True, "is_supported_wd": False,
        "active_provider": "pia", "sort_order": 17
    },
    {
        "balancer_tag": "EG", "country_code": "eg", "name_en": "Egypt", "name_fa": "مصر",
        "flag_emoji": "🇪🇬", "is_active": False, "is_supported_pia": True, "is_supported_wd": False,
        "active_provider": "pia", "sort_order": 18
    },
    {
        "balancer_tag": "GL", "country_code": "gl", "name_en": "Greenland", "name_fa": "گرینلند",
        "flag_emoji": "🇬🇱", "is_active": False, "is_supported_pia": True, "is_supported_wd": False,
        "active_provider": "pia", "sort_order": 19
    },
    {
        "balancer_tag": "GH", "country_code": "gh", "name_en": "Ghana", "name_fa": "غنا",
        "flag_emoji": "🇬🇭", "is_active": False, "is_supported_pia": True, "is_supported_wd": False,
        "active_provider": "pia", "sort_order": 20
    },
    {
        "balancer_tag": "AR", "country_code": "ae", "name_en": "UAE / Dubai", "name_fa": "امارات",
        "flag_emoji": "🇦🇪", "is_active": False, "is_supported_pia": True, "is_supported_wd": False,
        "active_provider": "pia", "sort_order": 21
    },
    # --- Additional Catalog Locations (Ready to Activate Anytime) ---
    {
        "balancer_tag": "JP", "country_code": "jp", "name_en": "Japan", "name_fa": "ژاپن",
        "flag_emoji": "🇯🇵", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 22
    },
    {
        "balancer_tag": "SG", "country_code": "sg", "name_en": "Singapore", "name_fa": "سنگاپور",
        "flag_emoji": "🇸🇬", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 23
    },
    {
        "balancer_tag": "FI", "country_code": "fi", "name_en": "Finland", "name_fa": "فنلاند",
        "flag_emoji": "🇫🇮", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 24
    },
    {
        "balancer_tag": "AT", "country_code": "at", "name_en": "Austria", "name_fa": "اتریش",
        "flag_emoji": "🇦🇹", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 25
    },
    {
        "balancer_tag": "AU", "country_code": "au", "name_en": "Australia", "name_fa": "استرالیا",
        "flag_emoji": "🇦🇺", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 26
    },
    {
        "balancer_tag": "BE", "country_code": "be", "name_en": "Belgium", "name_fa": "بلژیک",
        "flag_emoji": "🇧🇪", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 27
    },
    {
        "balancer_tag": "BR", "country_code": "br", "name_en": "Brazil", "name_fa": "برزیل",
        "flag_emoji": "🇧🇷", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 28
    },
    {
        "balancer_tag": "BG", "country_code": "bg", "name_en": "Bulgaria", "name_fa": "بلغارستان",
        "flag_emoji": "🇧🇬", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 29
    },
    {
        "balancer_tag": "CZ", "country_code": "cz", "name_en": "Czech Republic", "name_fa": "جمهوری چک",
        "flag_emoji": "🇨🇿", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 30
    },
    {
        "balancer_tag": "GR", "country_code": "gr", "name_en": "Greece", "name_fa": "یونان",
        "flag_emoji": "🇬🇷", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 31
    },
    {
        "balancer_tag": "HU", "country_code": "hu", "name_en": "Hungary", "name_fa": "مجارستان",
        "flag_emoji": "🇭🇺", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 32
    },
    {
        "balancer_tag": "IE", "country_code": "ie", "name_en": "Ireland", "name_fa": "ایرلند",
        "flag_emoji": "🇮🇪", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 33
    },
    {
        "balancer_tag": "IL", "country_code": "il", "name_en": "Israel", "name_fa": "اسرائیل",
        "flag_emoji": "🇮🇱", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 34
    },
    {
        "balancer_tag": "IN", "country_code": "in", "name_en": "India", "name_fa": "هند",
        "flag_emoji": "🇮🇳", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 35
    },
    {
        "balancer_tag": "MX", "country_code": "mx", "name_en": "Mexico", "name_fa": "مکزیک",
        "flag_emoji": "🇲🇽", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 36
    },
    {
        "balancer_tag": "NZ", "country_code": "nz", "name_en": "New Zealand", "name_fa": "نیوزیلند",
        "flag_emoji": "🇳🇿", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 37
    },
    {
        "balancer_tag": "PT", "country_code": "pt", "name_en": "Portugal", "name_fa": "پرتغال",
        "flag_emoji": "🇵🇹", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 38
    },
    {
        "balancer_tag": "UA", "country_code": "ua", "name_en": "Ukraine", "name_fa": "اوکراین",
        "flag_emoji": "🇺🇦", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 39
    },
    {
        "balancer_tag": "ZA", "country_code": "za", "name_en": "South Africa", "name_fa": "آفریقای جنوبی",
        "flag_emoji": "🇿🇦", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 40
    },
    {
        "balancer_tag": "IS", "country_code": "is", "name_en": "Iceland", "name_fa": "ایسلند",
        "flag_emoji": "🇮🇸", "is_active": False, "is_supported_pia": True, "is_supported_wd": False,
        "active_provider": "pia", "sort_order": 41
    },
    {
        "balancer_tag": "CO", "country_code": "co", "name_en": "Colombia", "name_fa": "کلمبیا",
        "flag_emoji": "🇨🇴", "is_active": False, "is_supported_pia": False, "is_supported_wd": True,
        "active_provider": "windscribe", "sort_order": 42
    },
    {
        "balancer_tag": "HK", "country_code": "hk", "name_en": "Hong Kong", "name_fa": "هنگ‌کنگ",
        "flag_emoji": "🇭🇰", "is_active": False, "is_supported_pia": True, "is_supported_wd": True,
        "active_provider": "both", "sort_order": 43
    },
]

DEFAULT_ACTIVE_TAGS: List[str] = []


class BalancerLocationService:
    @classmethod
    async def initialize_and_seed(cls, session=None) -> int:
        """Seeds the balancer_locations table if empty or updates missing items."""
        async def _seed(db):
            existing = (await db.execute(select(BalancerLocation))).scalars().all()
            existing_tags = {loc.balancer_tag: loc for loc in existing}
            added_count = 0

            for item in MASTER_LOCATION_CATALOG:
                tag = item["balancer_tag"]
                if tag not in existing_tags:
                    loc = BalancerLocation(
                        balancer_tag=tag,
                        country_code=item["country_code"],
                        name_en=item["name_en"],
                        name_fa=item["name_fa"],
                        flag_emoji=item["flag_emoji"],
                        is_active=item["is_active"],
                        is_supported_pia=item["is_supported_pia"],
                        is_supported_wd=item["is_supported_wd"],
                        active_provider=item["active_provider"],
                        sort_order=item["sort_order"],
                    )
                    db.add(loc)
                    added_count += 1
                else:
                    # Update supported flags if changed in catalog
                    loc = existing_tags[tag]
                    loc.is_supported_pia = item["is_supported_pia"]
                    loc.is_supported_wd = item["is_supported_wd"]
                    loc.name_fa = item["name_fa"]
                    loc.name_en = item["name_en"]
                    loc.flag_emoji = item["flag_emoji"]

            await db.commit()
            if added_count > 0:
                logger.info(f"Seeded {added_count} new balancer locations into database.")
            return added_count

        if session:
            return await _seed(session)
        async with AsyncSessionLocal() as db:
            return await _seed(db)

    @classmethod
    async def get_all_locations(cls, session=None) -> List[Dict[str, Any]]:
        """Returns all balancer locations with coverage badges and status."""
        async def _get(db):
            result = (await db.execute(
                select(BalancerLocation).order_by(BalancerLocation.sort_order.asc(), BalancerLocation.balancer_tag.asc())
            )).scalars().all()

            if not result:
                await cls.initialize_and_seed(db)
                result = (await db.execute(
                    select(BalancerLocation).order_by(BalancerLocation.sort_order.asc(), BalancerLocation.balancer_tag.asc())
                )).scalars().all()

            items = []
            for loc in result:
                # Determine provider badge
                if loc.is_supported_pia and loc.is_supported_wd:
                    coverage_type = "BOTH"
                    coverage_label = "PIA + Windscribe"
                    coverage_badge = "🟢 مشترک (PIA + WD)"
                elif loc.is_supported_pia:
                    coverage_type = "PIA"
                    coverage_label = "PIA Only"
                    coverage_badge = "🔵 فقط PIA"
                elif loc.is_supported_wd:
                    coverage_type = "WD"
                    coverage_label = "Windscribe Only"
                    coverage_badge = "🟣 فقط WD"
                else:
                    coverage_type = "NONE"
                    coverage_label = "None"
                    coverage_badge = "⚪️ نامشخص"

                items.append({
                    "id": str(loc.id),
                    "balancer_tag": loc.balancer_tag,
                    "country_code": loc.country_code,
                    "name_en": loc.name_en,
                    "name_fa": loc.name_fa,
                    "flag_emoji": loc.flag_emoji,
                    "is_active": loc.is_active,
                    "is_supported_pia": loc.is_supported_pia,
                    "is_supported_wd": loc.is_supported_wd,
                    "active_provider": loc.active_provider,
                    "coverage_type": coverage_type,
                    "coverage_label": coverage_label,
                    "coverage_badge": coverage_badge,
                    "sort_order": loc.sort_order,
                })
            return items

        if session:
            return await _get(session)
        async with AsyncSessionLocal() as db:
            return await _get(db)

    @classmethod
    async def get_active_tags(cls, provider: Optional[str] = None, session=None) -> List[str]:
        """
        Dynamically returns list of active balancer tags from database.
        Optionally filters by provider ('pia' or 'windscribe').
        Falls back safely to DEFAULT_ACTIVE_TAGS if database is not available.
        """
        try:
            async def _fetch(db):
                query = select(BalancerLocation).where(BalancerLocation.is_active == True)
                if provider:
                    p = provider.lower().strip()
                    if p == "pia":
                        query = query.where(
                            BalancerLocation.is_supported_pia == True,
                            BalancerLocation.active_provider.in_(["both", "pia"])
                        )
                    elif p in ("windscribe", "wd"):
                        query = query.where(
                            BalancerLocation.is_supported_wd == True,
                            BalancerLocation.active_provider.in_(["both", "windscribe"])
                        )
                locs = (await db.execute(query)).scalars().all()
                if locs:
                    return [loc.balancer_tag for loc in locs]
                return None

            if session:
                tags = await _fetch(session)
            else:
                async with AsyncSessionLocal() as db:
                    tags = await _fetch(db)

            if tags:
                return tags
        except Exception as exc:
            logger.warning(f"Could not load dynamic balancer tags from database: {exc}. Using fallback.")

        # Safe fallback
        if provider and provider.lower() == "pia":
            return [t for t in DEFAULT_ACTIVE_TAGS if t not in ["SE", "TR"]]
        return list(DEFAULT_ACTIVE_TAGS)

    @classmethod
    def get_active_tags_sync(cls, provider: Optional[str] = None) -> List[str]:
        """Synchronous wrapper for get_active_tags using a temporary SQLite connection or fallback."""
        import sqlite3
        import os
        db_path = os.getenv("DATABASE_URL", "")
        # If SQLite path is available in sqlite:/// format
        raw_path = None
        if "sqlite:///" in db_path:
            raw_path = db_path.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
        elif os.path.exists("vpn_private.db"):
            raw_path = "vpn_private.db"
        elif os.path.exists("/app/vpn_private.db"):
            raw_path = "/app/vpn_private.db"
        elif os.path.exists("/opt/vpn-private/vpn_private.db"):
            raw_path = "/opt/vpn-private/vpn_private.db"

        if raw_path and os.path.exists(raw_path):
            try:
                conn = sqlite3.connect(raw_path)
                cursor = conn.cursor()
                if provider:
                    p = provider.lower().strip()
                    if p == "pia":
                        rows = cursor.execute(
                            "SELECT balancer_tag FROM balancer_locations WHERE is_active=1 AND is_supported_pia=1 AND active_provider IN ('both', 'pia')"
                        ).fetchall()
                    elif p in ("windscribe", "wd"):
                        rows = cursor.execute(
                            "SELECT balancer_tag FROM balancer_locations WHERE is_active=1 AND is_supported_wd=1 AND active_provider IN ('both', 'windscribe')"
                        ).fetchall()
                    else:
                        rows = cursor.execute("SELECT balancer_tag FROM balancer_locations WHERE is_active=1").fetchall()
                else:
                    rows = cursor.execute("SELECT balancer_tag FROM balancer_locations WHERE is_active=1").fetchall()
                conn.close()
                if rows:
                    return [r[0] for r in rows]
            except Exception:
                pass

        if provider and provider.lower() == "pia":
            return [t for t in DEFAULT_ACTIVE_TAGS if t not in ["SE", "TR"]]
        return list(DEFAULT_ACTIVE_TAGS)

    @classmethod
    async def toggle_location(cls, balancer_tag: str, is_active: bool, session=None) -> Dict[str, Any]:
        """Toggles a balancer location active state in database."""
        async def _toggle(db):
            loc = (await db.execute(
                select(BalancerLocation).where(BalancerLocation.balancer_tag == balancer_tag)
            )).scalar_one_or_none()

            if not loc:
                return {"success": False, "error": f"لوکیشن {balancer_tag} یافت نشد."}

            loc.is_active = is_active
            await db.commit()
            return {
                "success": True,
                "balancer_tag": loc.balancer_tag,
                "name_fa": loc.name_fa,
                "is_active": loc.is_active,
                "flag_emoji": loc.flag_emoji,
            }

        if session:
            return await _toggle(session)
        async with AsyncSessionLocal() as db:
            return await _toggle(db)

    @classmethod
    async def set_active_provider(cls, balancer_tag: str, active_provider: str, session=None) -> Dict[str, Any]:
        """Sets active provider mode for a balancer ('both', 'pia', 'windscribe')."""
        val = active_provider.lower().strip()
        if val not in ("both", "pia", "windscribe"):
            val = "both"

        async def _set_mode(db):
            loc = (await db.execute(
                select(BalancerLocation).where(BalancerLocation.balancer_tag == balancer_tag)
            )).scalar_one_or_none()

            if not loc:
                return {"success": False, "error": f"لوکیشن {balancer_tag} یافت نشد."}

            loc.active_provider = val
            await db.commit()
            return {
                "success": True,
                "balancer_tag": loc.balancer_tag,
                "active_provider": loc.active_provider,
            }

        if session:
            return await _set_mode(session)
        async with AsyncSessionLocal() as db:
            return await _set_mode(db)

    @classmethod
    async def get_summary(cls, session=None) -> Dict[str, Any]:
        """Returns statistics of total, active, both, pia, and windscribe locations."""
        locations = await cls.get_all_locations(session=session)
        total = len(locations)
        active = sum(1 for l in locations if l["is_active"])
        both = sum(1 for l in locations if l["coverage_type"] == "BOTH")
        pia_only = sum(1 for l in locations if l["coverage_type"] == "PIA")
        wd_only = sum(1 for l in locations if l["coverage_type"] == "WD")

        return {
            "total_locations": total,
            "active_locations": active,
            "both_count": both,
            "pia_only_count": pia_only,
            "wd_only_count": wd_only,
            "active_tags": [l["balancer_tag"] for l in locations if l["is_active"]],
        }
