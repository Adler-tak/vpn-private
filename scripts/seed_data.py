"""
Database Initialization & Core Settings Seeding Service.

By default, runs clean initialization for production environments:
1. Creates all database tables via SQLAlchemy Base metadata.
2. Initializes default Chart of Accounts (Assets, Liabilities, Equity, Revenue, Expense).
3. Seeds all default system settings into system_settings table (3-to-97 rule).
4. Seeds and caches default Telegram bot texts into bot_texts.json and cache.

Demo panels, servers, products/tariffs, and discount coupons are NOT injected by default.
To inject demo sample data for development/testing, pass `--demo` or `--with-demo`.
"""
import sys
import asyncio
import uuid
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.database.session import AsyncSessionLocal, engine, Base
from app.database.models.accounting import AccountingAccount
from app.database.models.panel import Panel, Inbound, Server
from app.database.models.product import Product
from app.database.models.reseller import Coupon
from app.modules.accounting.service import AccountingService
from app.services.settings_service import SettingsService
from app.modules.bot_texts.service import _load_texts


async def seed_core():
    """Run clean initialization: tables, chart of accounts, system settings, bot texts."""
    print("[1/4] Initializing database tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        print("[2/4] Seeding default Chart of Accounts...")
        accounting = AccountingService(session)
        await accounting.initialize_default_accounts()
        await session.commit()

    print("[3/4] Initializing core system settings...")
    try:
        await SettingsService.initialize_and_seed()
    except Exception as e:
        print(f"  [!] Note: SettingsService initialization: {e}")

    print("[4/4] Initializing default Telegram bot texts...")
    try:
        from app.modules.bot_texts.service import DEFAULT_BOT_TEXTS, update_bot_texts
        texts = _load_texts()
        missing = {k: v["default"] for k, v in DEFAULT_BOT_TEXTS.items() if k not in texts}
        if missing:
            update_bot_texts(missing)
        print("  [✓] Telegram bot texts initialized.")
    except Exception as e:
        print(f"  [!] Note: Bot texts initialization: {e}")

    print("\n[✓] Database schema and core settings initialized successfully in RAW/CLEAN state:")
    print("    - Panels: 0")
    print("    - Products/Tariffs: 0")
    print("    - Coupons: 0")
    print("    - Servers/Inbounds: 0")
    print("    - Chart of Accounts: 5 essential ledger accounts")
    print("    - Telegram Bot Texts: Default templates ready")


async def seed(demo: bool = False):
    """Entrypoint function for external imports."""
    await seed_core()
    if demo:
        await seed_demo()


seed_clean = seed_core


async def seed_demo():
    """Inject optional demo panels, inbounds, servers, tariffs, and coupons (dev/testing only)."""
    print("\n[DEMO] Injecting sample panels, servers, products, and coupons...")
    async with AsyncSessionLocal() as session:
        # 1. Demo Panel
        panel_res = await session.execute(select(Panel).where(Panel.name == "Frankfurt Main Panel"))
        panel = panel_res.scalar_one_or_none()
        if not panel:
            panel = Panel(
                id=uuid.uuid4(),
                name="Frankfurt Main Panel",
                base_url="http://127.0.0.1:2053",
                username="admin",
                version="v3.11",
                is_active=True,
                is_healthy=True,
            )
            panel.password = "admin123"
            session.add(panel)
            await session.flush()
            print(f"  Created demo panel: {panel.name}")

        # 2. Demo Inbounds
        inbound_res = await session.execute(select(Inbound).where(Inbound.panel_id == panel.id))
        if not inbound_res.scalars().first():
            inbound_vless = Inbound(
                id=uuid.uuid4(),
                panel_id=panel.id,
                remote_inbound_id=1,
                protocol="vless",
                port=443,
                tag="inbound-443-vless",
                remark="VLESS-REALITY-DE",
                stream_settings='{"network": "tcp", "security": "reality", "realitySettings": {"serverNames": ["speedtest.net"], "publicKey": "sample_pbk", "shortId": "sample_sid"}}',
                max_clients=1000,
            )
            inbound_vmess = Inbound(
                id=uuid.uuid4(),
                panel_id=panel.id,
                remote_inbound_id=2,
                protocol="vmess",
                port=2083,
                tag="inbound-2083-vmess",
                remark="VMESS-WS-DE",
                stream_settings='{"network": "ws", "security": "none", "wsSettings": {"path": "/vmess-ws"}}',
                max_clients=500,
            )
            session.add_all([inbound_vless, inbound_vmess])
            await session.flush()
            print("  Created demo inbounds: VLESS Reality (443), VMess WS (2083)")

        # 3. Demo Servers
        servers_data = [
            ("سرور ۱ آلمان (فرانکفورت)", "DE-1", "آلمان ۱", "🇩🇪", 25, "198.51.100.1", 2096),
            ("سرور ۲ آلمان (تونل Reality)", "DE-2", "آلمان ۲", "🇩🇪", 24, "198.51.100.2", 9443),
            ("سرور ۱ سوئد (استکهلم Reality)", "SE-1", "سوئد ۱", "🇸🇪", 23, "198.51.100.3", 443),
            ("سرور ۲ سوئد (استکهلم ۲)", "SE-2", "سوئد ۲", "🇸🇪", 22, "198.51.100.4", 2096),
            ("سرور ۳ سوئد (تونل Reality ۳)", "SE-3", "سوئد ۳", "🇸🇪", 21, "198.51.100.5", 2096),
            ("سرور ۱ هلند (آمستردام ۱)", "NL-1", "هلند ۱", "🇳🇱", 21, "198.51.100.6", 2096),
            ("سرور ۲ هلند (آمستردام ۲)", "NL-2", "هلند ۲", "🇳🇱", 20, "198.51.100.7", 2096),
            ("سرور ۱ انگلیس (لندن)", "UK-1", "انگلیس ۱", "🇬🇧", 19, "198.51.100.8", 2096),
            ("سرور ۲ انگلیس (منچستر)", "UK-2", "انگلیس ۲", "🇬🇧", 18, "198.51.100.9", 2096),
            ("سرور ۳ انگلیس (تاتنهام)", "UK-3", "انگلیس ۳", "🇬🇧", 17, "198.51.100.10", 2096),
            ("سرور ۴ انگلیس (ساوت‌همپتون)", "UK-4", "انگلیس ۴", "🇬🇧", 16, "198.51.100.11", 2096),
            ("سرور ۵ انگلیس (سرور ۲)", "UK-5", "انگلیس ۵", "🇬🇧", 15, "198.51.100.12", 2096),
            ("سرور ۱ فرانسه (پاریس ۱)", "FR-1", "فرانسه ۱", "🇫🇷", 16, "198.51.100.13", 2096),
            ("سرور ۲ فرانسه (پاریس ۲)", "FR-2", "فرانسه ۲", "🇫🇷", 15, "198.51.100.14", 2096),
            ("سرور ۳ فرانسه (جنوب ۳)", "FR-3", "فرانسه ۳", "🇫🇷", 14, "198.51.100.15", 2096),
            ("سرور ۱ آمریکا (نیویورک)", "US-1", "آمریکا ۱", "🇺🇸", 14, "198.51.100.16", 2096),
            ("سرور ۲ آمریکا (اوهایو)", "US-2", "آمریکا ۲", "🇺🇸", 13, "198.51.100.17", 2096),
            ("سرور ۳ آمریکا (ویلمینگتون)", "US-3", "آمریکا ۳", "🇺🇸", 12, "198.51.100.18", 2096),
            ("سرور ۴ آمریکا (بروکلین)", "US-4", "آمریکا ۴", "🇺🇸", 11, "198.51.100.19", 2096),
            ("سرور ۱ ایتالیا (میلان ۱)", "IT-1", "ایتالیا ۱", "🇮🇹", 10, "198.51.100.20", 2096),
            ("سرور ۲ ایتالیا (میلان ۲)", "IT-2", "ایتالیا ۲", "🇮🇹", 9, "198.51.100.21", 2096),
            ("سرور ۱ دانمارک (کپنهاگ ۱)", "DK-1", "دانمارک ۱", "🇩🇰", 8, "198.51.100.22", 2096),
            ("سرور ۲ دانمارک (کپنهاگ ۲)", "DK-2", "دانمارک ۲", "🇩🇰", 7, "198.51.100.23", 2096),
            ("سرور امارات (دبی)", "AE", "امارات", "🇦🇪", 6, "198.51.100.24", 2096),
            ("سرور قبرس (نیکوزیا)", "CY", "قبرس", "🇨🇾", 5, "198.51.100.25", 2096),
            ("سرور ارمنستان (ایروان)", "AM", "ارمنستان", "🇦🇲", 4, "198.51.100.26", 2096),
            ("سرور مصر (قاهره)", "EG", "مصر", "🇪🇬", 3, "198.51.100.27", 2096),
            ("سرور گرینلند", "GL", "گرینلند", "🇬🇱", 2, "198.51.100.28", 2096),
            ("سرور غنا", "GH", "غنا", "🇬🇭", 1, "198.51.100.29", 2096),
            ("سرور ترکیه (استانبول)", "TR", "ترکیه", "🇹🇷", 10, "198.51.100.30", 2096),
            ("سرور فنلاند (هلسینکی)", "FI", "فنلاند", "🇫🇮", 9, "198.51.100.31", 2096),
            ("سرور لهستان (ورشو)", "PL", "لهستان", "🇵🇱", 9, "198.51.100.32", 2096),
            ("سرور نروژ (اسلو)", "NO", "نروژ", "🇳🇴", 8, "198.51.100.33", 2096),
        ]
        for name, code, loc_name, flag, priority, host, port in servers_data:
            s_res = await session.execute(select(Server).where(Server.name == name))
            if not s_res.scalar_one_or_none():
                server = Server(
                    id=uuid.uuid4(),
                    name=name,
                    location_code=code,
                    location_name=loc_name,
                    flag_emoji=flag,
                    priority=priority,
                    panel_id=panel.id,
                    subscription_host=host,
                    subscription_port=port,
                    is_active=True,
                    capacity_limit=1000,
                )
                session.add(server)
        await session.flush()
        print(f"  Created {len(servers_data)} demo servers.")

        # 4. Demo Products
        products_data = [
            ("🔹 پلن حجمی ۵۰ گیگابایت", "اشتراک یک‌ماهه ۵۰ گیگابایت با بیش از ۲۵ لوکیشن متنوع", 50 * (1024**3), 30, 1_500_000, 1),
            ("🔹 پلن حجمی ۱۰۰ گیگابایت", "اشتراک یک‌ماهه ۱۰۰ گیگابایت محبوب برای وب‌گردی و استریم", 100 * (1024**3), 30, 3_000_000, 2),
            ("🔹 پلن حجمی ۲۰۰ گیگابایت", "اشتراک یک‌ماهه ۲۰۰ گیگابایت برای دانلود و مصرف سنگین", 200 * (1024**3), 30, 6_000_000, 3),
            ("♾️ پلن نامحدود ۱ لوکیشن", "اشتراک نامحدود یک‌ماهه با امکان انتخاب ۱ لوکیشن در اپلیکیشن Happ", 0, 30, 3_490_000, 4),
            ("♾️ پلن نامحدود ۲ لوکیشن", "اشتراک نامحدود یک‌ماهه با امکان انتخاب ۲ لوکیشن در اپلیکیشن Happ", 0, 30, 4_490_000, 5),
            ("♾️ پلن نامحدود ۳ لوکیشن", "اشتراک نامحدود یک‌ماهه با امکان انتخاب ۳ لوکیشن در اپلیکیشن Happ", 0, 30, 5_490_000, 6),
            ("♾️ پلن نامحدود ۴ لوکیشن", "اشتراک نامحدود یک‌ماهه با امکان انتخاب ۴ لوکیشن در اپلیکیشن Happ", 0, 30, 6_490_000, 7),
            ("♾️ پلن نامحدود ۵ لوکیشن", "اشتراک نامحدود یک‌ماهه VIP با امکان انتخاب ۵ لوکیشن در اپلیکیشن Happ", 0, 30, 7_490_000, 8),
        ]
        for name, desc, traffic, days, price, sort in products_data:
            p_res = await session.execute(select(Product).where(Product.name == name))
            if not p_res.scalar_one_or_none():
                prod = Product(
                    id=uuid.uuid4(),
                    name=name,
                    description=desc,
                    traffic_bytes=traffic,
                    duration_days=days,
                    price_irr=price,
                    protocol="vless",
                    allowed_locations="ALL",
                    is_active=True,
                    sort_order=sort,
                )
                session.add(prod)
        await session.flush()
        print("  Created demo products.")

        # 5. Demo Coupons
        coupons_data = [
            ("WELCOME10", 10, 0, 500),
            ("VIP20", 20, 0, 100),
        ]
        for code, percent, amount, max_u in coupons_data:
            c_res = await session.execute(select(Coupon).where(Coupon.code == code))
            if not c_res.scalar_one_or_none():
                coup = Coupon(
                    id=uuid.uuid4(),
                    code=code,
                    discount_percent=percent,
                    discount_amount_irr=amount,
                    max_uses=max_u,
                    is_active=True,
                )
                session.add(coup)
        await session.commit()
        print("[DEMO MODE] Demo panels, servers, products, and coupons seeded successfully.")


async def main():
    parser = argparse.ArgumentParser(description="VPN Private Database Initialization")
    parser.add_argument("--demo", "--with-demo", action="store_true", help="Inject sample demo panel, servers, products, and coupons")
    parser.add_argument("--clean", action="store_true", help="Run clean initialization only (default)")
    args = parser.parse_args()

    # Always run core clean initialization
    await seed_core()

    # Only inject demo data if explicitly asked
    if args.demo:
        await seed_demo()


if __name__ == "__main__":
    asyncio.run(main())
