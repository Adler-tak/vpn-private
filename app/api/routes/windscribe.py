"""
API router for Windscribe administration, authentication with 2FA, and dynamic sync.
"""

from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.auth import get_current_admin
from app.database.models.user import User
from app.modules.windscribe.service import WindscribeSyncService

router = APIRouter()


class WindscribeLoginRequest(BaseModel):
    username: str = Field(..., description="Windscribe username or email")
    password: str = Field(..., description="Windscribe password")
    otp_code: Optional[str] = Field(None, description="Optional 2FA/TOTP 6-digit verification code")


class WindscribeVerify2FARequest(BaseModel):
    otp_code: str = Field(..., description="2FA/TOTP 6-digit verification code")
    username: Optional[str] = Field(None, description="Windscribe username (optional if stored)")
    password: Optional[str] = Field(None, description="Windscribe password (optional if stored)")


class WindscribeImportConfigRequest(BaseModel):
    config_text: str = Field(..., description="WireGuard configuration in .conf, JSON, or key-value format")


@router.post("/import-config")
async def windscribe_import_config(
    req: WindscribeImportConfigRequest,
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """
    Import and validate a Windscribe WireGuard config (.conf or JSON),
    saving keys to the database and runtime configuration.
    """
    return WindscribeSyncService.import_wireguard_config(req.config_text)


@router.post("/auto-detect")
async def windscribe_auto_detect(
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """
    Auto-detect active Windscribe WireGuard keys from existing Xray configurations
    (such as WG-SW-GEMINI) and persist to database.
    """
    return WindscribeSyncService.auto_detect_wireguard_keys()


@router.get("/config")
async def windscribe_get_config(
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """
    Get current configured WireGuard keys and network addresses.
    """
    sk, addr, pub, psk, dns = WindscribeSyncService.get_wireguard_keys()
    return {
        "has_keys": bool(sk and (pub or psk)),
        "secret_key_preview": (sk[:6] + "..." + sk[-4:]) if sk and len(sk) > 10 else ("***" if sk else ""),
        "address": addr,
        "remote_dns": dns,
        "has_preshared_key": bool(psk),
        "server_pubkey": pub,
    }


@router.post("/login")
async def windscribe_login(
    req: WindscribeLoginRequest,
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """
    Authenticate platform admin with Windscribe API.
    If 2FA is required and otp_code is omitted, returns status='2fa_required'.
    """
    return WindscribeSyncService.login(
        username=req.username,
        password=req.password,
        otp_code=req.otp_code,
    )


@router.post("/verify-2fa")
async def windscribe_verify_2fa(
    req: WindscribeVerify2FARequest,
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """
    Verify Windscribe 2FA/OTP challenge and finalize session authentication.
    """
    username = req.username
    password = req.password

    if not username or not password:
        stored_user, stored_pass, _ = WindscribeSyncService.get_credentials()
        username = username or stored_user
        password = password or stored_pass

    if not username or not password:
        return {
            "status": "error",
            "message": "نام کاربری و رمز عبور یافت نشد. لطفاً مجدداً با نام کاربری و رمز عبور اقدام فرمایید.",
        }

    return WindscribeSyncService.login(
        username=username,
        password=password,
        otp_code=req.otp_code,
    )


@router.post("/sync")
async def windscribe_sync(
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """
    Trigger asynchronous background sync of Windscribe WireGuard candidate nodes.
    """
    return WindscribeSyncService.start_background_sync()


@router.get("/status")
@router.get("/sync/status")
async def windscribe_status(
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    """
    Get live status, progress, and result of Windscribe WireGuard synchronization.
    """
    return WindscribeSyncService.get_status()
