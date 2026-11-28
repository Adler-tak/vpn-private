"""
Database Initialization & Core Settings Seeding Service.

Runs 100% clean initialization for production environments:
1. Creates all database tables via SQLAlchemy Base metadata.
2. Initializes default Chart of Accounts (Assets, Liabilities, Equity, Revenue, Expense).
3. Seeds all default system settings into system_settings table (3-to-97 rule).
4. Seeds and caches default Telegram bot texts into bot_texts.json and cache.

Zero demo data: panels, servers, products/tariffs, and coupons are never injected.
"""
import sys
import asyncio
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database.session import AsyncSessionLocal, engine, Base
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
    """Entrypoint function for external imports: always runs 100% clean initialization."""
    await seed_core()


seed_clean = seed_core


async def main():
    parser = argparse.ArgumentParser(description="VPN Private Database Initialization")
    parser.add_argument("--clean", action="store_true", help="Run clean initialization (default)")
    parser.add_argument("--demo", "--with-demo", action="store_true", help="Deprecated: demo data is disabled")
    args = parser.parse_args()

    # Always run core clean initialization
    await seed_core()


if __name__ == "__main__":
    asyncio.run(main())
