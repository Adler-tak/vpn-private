"""Telegram Bot entrypoint, router binding, and middleware configuration."""
import asyncio
import logging
from typing import Optional, Tuple
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from app.core.config import settings
from app.bot.middlewares.auth import UserAuthMiddleware
from app.bot.middlewares.channel_lock import MandatoryChannelJoinMiddleware
from app.bot.handlers import start, purchase, services, wallet, referral, support, admin, voucher

logger = logging.getLogger(__name__)


def get_active_bot_config() -> Tuple[str, Optional[str]]:
    """Fetch active bot token and proxy_url synchronously from DB or settings."""
    token = settings.BOT_TOKEN
    proxy = settings.TELEGRAM_PROXY if getattr(settings, "TELEGRAM_PROXY_ENABLED", False) else None
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        from app.database.models.bot import TelegramBot

        sync_url = getattr(settings, "DATABASE_URL_SYNC", "")
        if "sqlite" in settings.DATABASE_URL and "sqlite" not in sync_url:
            sync_url = settings.DATABASE_URL.replace("sqlite+aiosqlite", "sqlite")
        elif "postgresql" in settings.DATABASE_URL and "postgresql" not in sync_url:
            sync_url = settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql")

        if "sqlite" in sync_url:
            engine = create_engine(sync_url, connect_args={"check_same_thread": False})
        else:
            engine = create_engine(sync_url, pool_pre_ping=True)

        with Session(engine) as session:
            stmt = select(TelegramBot).where(TelegramBot.is_active == True).limit(1)
            bot_row = session.execute(stmt).scalar_one_or_none()
            if bot_row:
                if bot_row.token:
                    token = bot_row.token
                if bot_row.proxy_enabled and bot_row.proxy_url:
                    proxy = bot_row.proxy_url
    except Exception as e:
        logger.debug(f"Could not load active TelegramBot config from DB (sync): {e}")

    return token, proxy


async def get_active_bot_config_async() -> Tuple[str, Optional[str]]:
    """Fetch active bot token and proxy_url asynchronously from DB or settings."""
    token = settings.BOT_TOKEN
    proxy = settings.TELEGRAM_PROXY if getattr(settings, "TELEGRAM_PROXY_ENABLED", False) else None
    try:
        from sqlalchemy import select
        from app.database.session import AsyncSessionLocal
        from app.database.models.bot import TelegramBot

        async with AsyncSessionLocal() as session:
            stmt = select(TelegramBot).where(TelegramBot.is_active == True).limit(1)
            bot_row = (await session.execute(stmt)).scalar_one_or_none()
            if bot_row:
                if bot_row.token:
                    token = bot_row.token
                if bot_row.proxy_enabled and bot_row.proxy_url:
                    proxy = bot_row.proxy_url
    except Exception as e:
        logger.debug(f"Could not load active TelegramBot config from DB (async): {e}")

    return token, proxy


def get_active_bot_token() -> str:
    """Fetch active bot token."""
    token, _ = get_active_bot_config()
    return token


async def get_active_bot_token_async() -> str:
    """Fetch active bot token asynchronously."""
    token, _ = await get_active_bot_config_async()
    return token


def create_bot(token: Optional[str] = None, proxy: Optional[str] = None) -> Bot:
    """Instantiate Aiogram Bot instance with proxy support."""
    bot_token = get_active_bot_token()
    _, bot_proxy = get_active_bot_config()
    final_token = token or bot_token
    final_proxy = proxy if proxy is not None else bot_proxy

    bot_session = None
    if final_proxy and final_proxy.strip():
        bot_session = AiohttpSession(proxy=final_proxy.strip())
        logger.info(f"Configured Bot with proxy: {final_proxy.strip()}")

    return Bot(
        token=final_token,
        session=bot_session,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )


async def create_bot_async(token: Optional[str] = None, proxy: Optional[str] = None) -> Bot:
    """Instantiate Aiogram Bot instance asynchronously with proxy support."""
    bot_token, bot_proxy = await get_active_bot_config_async()
    final_token = token or bot_token
    final_proxy = proxy if proxy is not None else bot_proxy

    bot_session = None
    if final_proxy and final_proxy.strip():
        bot_session = AiohttpSession(proxy=final_proxy.strip())
        logger.info(f"Configured Bot asynchronously with proxy: {final_proxy.strip()}")

    return Bot(
        token=final_token,
        session=bot_session,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )


def create_dispatcher() -> Dispatcher:
    """Instantiate and configure Dispatcher with middlewares and routers."""
    dp = Dispatcher()

    # Outer middleware for DB user context
    dp.update.outer_middleware(UserAuthMiddleware())

    # Mandatory Channel Join Enforcement
    dp.message.middleware(MandatoryChannelJoinMiddleware())
    dp.callback_query.middleware(MandatoryChannelJoinMiddleware())

    # Register Routers
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(wallet.router)
    dp.include_router(voucher.router)
    dp.include_router(services.router)
    dp.include_router(purchase.router)
    dp.include_router(referral.router)
    dp.include_router(support.router)

    return dp


async def run_bot():
    """Run bot polling with active proxy settings."""
    try:
        from app.database.session import engine, Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            from app.services.settings_service import SettingsService
            await SettingsService.initialize_and_seed()
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"DB init check in bot runner: {e}")

    bot = await create_bot_async()
    dp = create_dispatcher()
    logger.info("Starting VPN Private Telegram Bot in long-polling mode...")

    # Configure Telegram Chat Menu Button to open Mini App directly
    try:
        from aiogram.types import MenuButtonWebApp, WebAppInfo
        from app.bot.keyboards.default import get_effective_webapp_url
        webapp_url = get_effective_webapp_url()
        if webapp_url.startswith("https://"):
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="App", web_app=WebAppInfo(url=webapp_url))
            )
            logger.info(f"Telegram Chat Menu Button set to WebApp: {webapp_url}")
    except Exception as mb_err:
        logger.debug(f"Could not set chat menu button: {mb_err}")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_bot())
