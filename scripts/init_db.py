"""
Database Initialization & Core Settings Script.

Initializes database schema, chart of accounts, default system settings,
and default bot texts cleanly without demo data.
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.seed_data import seed_core

if __name__ == "__main__":
    asyncio.run(seed_core())
