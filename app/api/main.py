"""FastAPI application entrypoint and route mounting."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.session import get_db
from app.core.exceptions import VPNPrivateError
from app.core.config import settings
from app.core.logging import setup_logging
from app.api.routes import health, payments, admin, windscribe, webapp

logger = setup_logging()


async def _background_traffic_sync_loop():
    """Periodically sync real traffic stats from active panels every 60 seconds."""
    from app.workers.tasks.sync import _async_sync_all_traffic
    await asyncio.sleep(5)
    while True:
        try:
            await _async_sync_all_traffic()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in background traffic sync loop: {e}")
        await asyncio.sleep(60)


async def _background_expiration_check_loop():
    """Periodically check and process expired subscriptions every 60 seconds (ensures time expirations are handled in docker-compose)."""
    from app.workers.tasks.sync import _async_check_expired
    await asyncio.sleep(10)
    while True:
        try:
            await _async_check_expired()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in background expiration check loop: {e}")
        await asyncio.sleep(60)


async def _background_inbound_sync_loop():
    """Periodically sync inbounds and metadata from active panels every 24 hours (preserving DB active states)."""
    from app.database.session import AsyncSessionLocal
    from app.database.models.panel import Panel
    from app.api.routes.admin import sync_panel_inbounds_endpoint
    from sqlalchemy import select

    # Initial delay: wait 60 seconds after startup before the first run
    await asyncio.sleep(60)
    while True:
        try:
            logger.info("Executing scheduled 24-hour 3X-UI inbound sync...")
            async with AsyncSessionLocal() as db:
                panels = (await db.execute(select(Panel).where(Panel.is_active == True))).scalars().all()
                class SystemAdmin:
                    telegram_id = 0
                    username = "system_scheduler"

                for p in panels:
                    try:
                        res = await sync_panel_inbounds_endpoint(panel_id=p.id, admin=SystemAdmin(), db=db)
                        logger.info(f"Auto-synced panel {p.name}: {res.get('synced_count', 0)} inbounds (DB active states preserved).")
                    except Exception as pe:
                        logger.error(f"Error auto-syncing panel {p.name}: {pe}")
                        try:
                            await db.rollback()
                        except Exception:
                            pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in background 24h inbound sync loop: {e}")

        # 24 hours = 86400 seconds
        await asyncio.sleep(86400)


async def _background_winback_loop():
    """Periodically execute win-back campaign checks every 2 hours."""
    from app.services.winback_service import WinbackService
    from app.database.session import AsyncSessionLocal

    await asyncio.sleep(120)  # Initial delay 2 mins after boot
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await WinbackService.process_winback_campaign(session=session)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in background winback loop: {e}")

        # 2 hours = 7200 seconds
        await asyncio.sleep(7200)


async def _background_smart_maintenance_loop():
    """Periodically check smart consumption alerts and auto-renewals every 15 minutes."""
    from app.services.smart_alert_service import SmartAlertService
    from app.services.auto_renew_service import AutoRenewService
    from app.database.session import AsyncSessionLocal

    await asyncio.sleep(60)  # Initial delay 1 min after boot
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await SmartAlertService.process_smart_alerts(session=session)
                await AutoRenewService.process_auto_renewals(session=session)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in background smart maintenance loop: {e}")

        # 15 minutes = 900 seconds
        await asyncio.sleep(900)


async def _background_panel_health_loop():
    """Periodically monitor panel connectivity & Xray core status every 60 seconds and notify admins on errors."""
    from app.services.panel_health_service import PanelHealthService
    from app.database.session import AsyncSessionLocal

    await asyncio.sleep(15)  # Initial delay 15 seconds after startup
    while True:
        try:
            async with AsyncSessionLocal() as session:
                await PanelHealthService.check_all_panels_and_alert(db_session=session)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in background panel health loop: {e}")

        # Check every 60 seconds
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting VPN Private API Service...")
    from app.core.security import validate_security_keys
    validate_security_keys()

    # Ensure all database tables exist automatically on startup
    try:
        from app.database.session import engine, Base
        from sqlalchemy import inspect, text

        def _ensure_schema_compatibility(sync_conn):
            inspector = inspect(sync_conn)
            if "panels" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("panels")]
                if "panel_type" not in cols:
                    sync_conn.execute(text("ALTER TABLE panels ADD COLUMN panel_type VARCHAR(32) DEFAULT '3x-ui'"))
                    sync_conn.execute(text("UPDATE panels SET panel_type = '3x-ui' WHERE panel_type IS NULL OR panel_type = ''"))
            if "servers" in inspector.get_table_names():
                srv_cols = [c["name"] for c in inspector.get_columns("servers")]
                if "inbound_id" not in srv_cols:
                    sync_conn.execute(text("ALTER TABLE servers ADD COLUMN inbound_id VARCHAR(36)"))
            if "products" in inspector.get_table_names():
                try:
                    sync_conn.execute(text("ALTER TABLE products ALTER COLUMN allowed_locations TYPE TEXT"))
                except Exception:
                    pass
                prod_cols = [c["name"] for c in inspector.get_columns("products")]
                if "location_mode" not in prod_cols:
                    try:
                        sync_conn.execute(text("ALTER TABLE products ADD COLUMN location_mode VARCHAR(32) DEFAULT 'all'"))
                        sync_conn.execute(text("UPDATE products SET location_mode = CASE WHEN allowed_locations IS NULL OR allowed_locations = 'ALL' THEN 'all' ELSE 'custom' END WHERE location_mode IS NULL"))
                    except Exception:
                        pass
            if "subscriptions" in inspector.get_table_names():
                sub_cols = [c["name"] for c in inspector.get_columns("subscriptions")]
                if "last_grace_at" not in sub_cols:
                    sync_conn.execute(text("ALTER TABLE subscriptions ADD COLUMN last_grace_at DATETIME"))
                if "auto_renew" not in sub_cols:
                    sync_conn.execute(text("ALTER TABLE subscriptions ADD COLUMN auto_renew BOOLEAN DEFAULT 0"))
                if "alert_traffic_sent" not in sub_cols:
                    sync_conn.execute(text("ALTER TABLE subscriptions ADD COLUMN alert_traffic_sent BOOLEAN DEFAULT 0"))
                if "alert_time_sent" not in sub_cols:
                    sync_conn.execute(text("ALTER TABLE subscriptions ADD COLUMN alert_time_sent BOOLEAN DEFAULT 0"))
                if "last_auto_renew_attempt" not in sub_cols:
                    sync_conn.execute(text("ALTER TABLE subscriptions ADD COLUMN last_auto_renew_attempt DATETIME"))
                if "allowed_locations" not in sub_cols:
                    sync_conn.execute(text("ALTER TABLE subscriptions ADD COLUMN allowed_locations TEXT"))

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_schema_compatibility)
        logger.info("Database schema initialized and verified successfully.")

        # Initialize and seed dynamic system settings table (3-to-97 rule)
        try:
            from app.services.settings_service import SettingsService
            await SettingsService.initialize_and_seed()
        except Exception as err:
            logger.warning(f"Could not seed system settings table: {err}")

        # Synchronize existing database inbounds into Server entities on startup dynamically
        try:
            from app.database.session import AsyncSessionLocal
            from app.modules.servers.allocation import sync_inbounds_to_servers
            from app.database.models.panel import Panel, Inbound
            from sqlalchemy import select
            async with AsyncSessionLocal() as init_db:
                panels = (await init_db.execute(select(Panel))).scalars().all()
                total_init_servers = 0
                for p in panels:
                    inbs = (await init_db.execute(select(Inbound).where(Inbound.panel_id == p.id))).scalars().all()
                    if inbs:
                        synced = await sync_inbounds_to_servers(init_db, p, inbs)
                        total_init_servers += len(synced)
                await init_db.commit()
                if total_init_servers > 0:
                    logger.info(f"Startup dynamically synced {total_init_servers} inbounds to servers.")
        except Exception as err:
            logger.warning(f"Could not run initial inbound-to-server startup sync: {err}")
    except Exception as e:
        logger.error(f"Error ensuring database tables on startup: {e}")

    sync_task = asyncio.create_task(_background_traffic_sync_loop())
    expiration_task = asyncio.create_task(_background_expiration_check_loop())
    inbound_sync_task = asyncio.create_task(_background_inbound_sync_loop())
    winback_task = asyncio.create_task(_background_winback_loop())
    smart_task = asyncio.create_task(_background_smart_maintenance_loop())
    panel_health_task = asyncio.create_task(_background_panel_health_loop())
    yield
    sync_task.cancel()
    expiration_task.cancel()
    inbound_sync_task.cancel()
    winback_task.cancel()
    smart_task.cancel()
    panel_health_task.cancel()
    try:
        await asyncio.gather(sync_task, expiration_task, inbound_sync_task, winback_task, smart_task, panel_health_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    logger.info("Shutting down VPN Private API Service...")


def create_app() -> FastAPI:
    is_prod = (getattr(settings, "ENVIRONMENT", "").lower() == "production") and not getattr(settings, "DEBUG", False)
    docs_url = None if is_prod else "/docs"
    redoc_url = None if is_prod else "/redoc"
    openapi_url = None if is_prod else "/openapi.json"

    app = FastAPI(
        title=settings.PROJECT_NAME,
        version="1.0.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )

    # In production, specify explicit allowed origins for security
    allowed_origins = [settings.SERVER_HOST] if (is_prod and settings.SERVER_HOST) else ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True if allowed_origins != ["*"] else False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
        allow_headers=["*"],
    )

    @app.exception_handler(VPNPrivateError)
    async def vpn_private_exception_handler(request: Request, exc: VPNPrivateError):
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc)},
        )

    app.include_router(health.router, tags=["Health"])
    app.include_router(payments.router, prefix=f"{settings.API_V1_STR}/payments", tags=["Payments"])
    app.include_router(admin.router, prefix=f"{settings.API_V1_STR}/admin", tags=["Admin Web Panel"])
    app.include_router(admin.router, prefix="/api/admin", tags=["Admin Web Panel (Compat)"], include_in_schema=False)
    app.include_router(windscribe.router, prefix=f"{settings.API_V1_STR}/admin/windscribe", tags=["Windscribe Admin"])
    app.include_router(webapp.router, prefix=f"{settings.API_V1_STR}/webapp", tags=["Telegram Mini App"])

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    @app.api_route("/admin", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    @app.api_route("/dashboard", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    @app.api_route("/static/admin.html", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    async def serve_admin_panel():
        admin_html = Path(__file__).resolve().parent.parent / "static" / "admin.html"
        if admin_html.exists():
            return HTMLResponse(content=admin_html.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Web Admin Panel Loading...</h1>")

    @app.api_route("/app", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    @app.api_route("/webapp", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    @app.api_route("/mini-app", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    @app.api_route("/static/webapp.html", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
    async def serve_telegram_webapp():
        webapp_html = Path(__file__).resolve().parent.parent / "static" / "webapp.html"
        if webapp_html.exists():
            return HTMLResponse(content=webapp_html.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Telegram Mini App Loading...</h1>")

    @app.get("/admin/2fa/setup", include_in_schema=False)
    async def direct_admin_2fa_setup(
        request: Request,
        db: AsyncSession = Depends(get_db),
    ):
        from app.api.routes.admin import setup_admin_2fa, get_optional_admin
        admin_user = await get_optional_admin(request=request, auth_header=None, db=db)
        return await setup_admin_2fa(regenerate=True, admin=admin_user, db=db)

    return app


app = create_app()
