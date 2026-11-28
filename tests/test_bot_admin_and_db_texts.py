"""Tests for Bot Admin access preservation and Database persistence of Bot Texts."""
import secrets
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.database.session import Base
from app.database.models.user import User, Wallet
from app.database.models.setting import SystemSetting
from app.core.constants import UserRole
from app.core.config import settings
from app.modules.users.service import UserService
from app.bot.handlers.admin import is_admin
from app.modules.bot_texts.service import get_bot_text, update_bot_texts, get_all_bot_texts, DEFAULT_BOT_TEXTS


@pytest.mark.asyncio
async def test_admin_role_preserved_on_register_or_get():
    """Ensure that a dynamic admin added via Web Panel is NOT demoted to CUSTOMER by register_or_get_user."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Create an admin user who is NOT in settings.ADMIN_TELEGRAM_IDS
        admin_tg_id = 987654321
        assert admin_tg_id not in settings.ADMIN_TELEGRAM_IDS

        admin_user = User(
            id=uuid.uuid4(),
            telegram_id=admin_tg_id,
            username="bot_manager",
            first_name="Manager",
            role=UserRole.ADMIN,
            referral_code=f"ref{secrets.token_hex(4)}",
        )
        session.add(admin_user)
        session.add(Wallet(user_id=admin_user.id, balance_irr=0))
        await session.commit()

        # Simulate user interacting with bot (/admin or /start)
        user_service = UserService(session)
        loaded_user, created = await user_service.register_or_get_user(
            telegram_id=admin_tg_id,
            username="bot_manager",
            first_name="Manager",
        )
        await session.commit()

        assert not created
        assert loaded_user.role == UserRole.ADMIN, f"Admin was demoted to {loaded_user.role}!"
        assert is_admin(loaded_user) is True, "is_admin check failed for DB admin user!"

    await engine.dispose()


@pytest.mark.asyncio
async def test_super_admin_env_promotion():
    """Ensure user listed in settings.ADMIN_TELEGRAM_IDS is promoted to SUPER_ADMIN."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    super_tg_id = 11223344
    orig_admins = list(settings.ADMIN_TELEGRAM_IDS)
    settings.ADMIN_TELEGRAM_IDS = [super_tg_id]

    try:
        async with async_session() as session:
            # Register user
            user_service = UserService(session)
            u, created = await user_service.register_or_get_user(telegram_id=super_tg_id)
            await session.commit()

            assert created
            assert u.role == UserRole.SUPER_ADMIN
            assert is_admin(u) is True
    finally:
        settings.ADMIN_TELEGRAM_IDS = orig_admins

    await engine.dispose()


def test_bot_texts_database_and_cache_sync():
    """Verify that updating bot texts updates cache and persists properly."""
    orig_welcome = get_bot_text("start_welcome")
    test_val = "تست خوش‌آمدگویی اختصاصی برای بررسی ذخیره در دیتابیس"

    try:
        updated = update_bot_texts({"start_welcome": test_val})
        assert updated["start_welcome"] == test_val

        # Verify get_bot_text returns the updated value
        loaded = get_bot_text("start_welcome")
        assert loaded == test_val

        # Verify get_all_bot_texts includes it
        all_texts = get_all_bot_texts()
        item = next((x for x in all_texts if x["key"] == "start_welcome"), None)
        assert item is not None
        assert item["current_value"] == test_val
    finally:
        update_bot_texts({"start_welcome": orig_welcome})
        assert get_bot_text("start_welcome") == orig_welcome
