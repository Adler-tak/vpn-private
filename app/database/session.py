"""Database engine and session management using SQLAlchemy 2.0 async."""
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.compiler import compiles
from app.core.config import settings


# Ensure PostgreSQL UUID and JSONB compile cleanly on SQLite for local dev/testing
@compiles(UUID, "sqlite")
def compile_uuid_sqlite(type_, compiler, **kw):
    return "VARCHAR(36)"


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy declarative models."""
    pass


# Engine configuration supporting PostgreSQL and SQLite
engine_kwargs = {
    "echo": settings.DEBUG,
}
if "sqlite" in settings.DATABASE_URL:
    engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    engine_kwargs.update({
        "pool_size": 20,
        "max_overflow": 10,
        "pool_pre_ping": True,
    })

engine = create_async_engine(
    settings.DATABASE_URL,
    **engine_kwargs
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# Synchronous session support (for CLI, background sync, or Celery)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_sync_engine = None
_SyncSessionLocal = None


def get_sync_session():
    global _sync_engine, _SyncSessionLocal
    if _SyncSessionLocal is None:
        sync_url = getattr(settings, "DATABASE_URL_SYNC", "")
        if not sync_url or "sqlite" in settings.DATABASE_URL:
            raw_url = str(settings.DATABASE_URL)
            if "sqlite" in raw_url:
                sync_url = raw_url.replace("sqlite+aiosqlite:///", "sqlite:///")
            else:
                sync_url = raw_url.replace("+asyncpg", "")
        _sync_engine = create_engine(sync_url)
        try:
            Base.metadata.create_all(bind=_sync_engine)
        except Exception:
            pass
        _SyncSessionLocal = sessionmaker(bind=_sync_engine, expire_on_commit=False)
    return _SyncSessionLocal()


sync_session_factory = get_sync_session
async_session_factory = AsyncSessionLocal


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for providing an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
