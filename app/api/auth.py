"""Admin Authentication and Session Management for Web Admin Panel."""
import time
import json
import hmac
import hashlib
import base64
import secrets
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.config import settings
from app.core.cache import security_store
from app.database.session import get_db
from app.database.models.user import User
from app.core.constants import UserRole

bearer_scheme = HTTPBearer(auto_error=False)


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64_decode(data: str) -> bytes:
    padding = 4 - (len(data) % 4)
    if padding and padding < 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data.encode("utf-8"))


def create_admin_token(telegram_id: int, username: str, role: str, expires_in_seconds: int = 86400 * 7) -> str:
    """Generate a tamper-proof HMAC-SHA256 signed session token."""
    now = int(time.time())
    payload = {
        "jti": secrets.token_hex(8),
        "tg_id": telegram_id,
        "username": username or "admin",
        "role": role,
        "iat": now,
        "exp": now + expires_in_seconds,
    }
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    encoded_payload = _b64_encode(raw_payload)
    
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    encoded_sig = _b64_encode(signature)
    return f"{encoded_payload}.{encoded_sig}"


def verify_admin_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify signature, expiration, and in-memory blacklist of session token."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        encoded_payload, encoded_sig = parts
        
        expected_sig = hmac.new(
            settings.SECRET_KEY.encode("utf-8"),
            encoded_payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        
        provided_sig = _b64_decode(encoded_sig)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return None
        
        payload_bytes = _b64_decode(encoded_payload)
        payload = json.loads(payload_bytes.decode("utf-8"))
        
        now = int(time.time())
        if payload.get("exp", 0) < now:
            return None
        if security_store._token_epoch and payload.get("iat", 0) <= security_store._token_epoch:
            return None
        if token in security_store._blacklisted_tokens and now < security_store._blacklisted_tokens[token]:
            return None
        return payload
    except Exception:
        return None


async def async_verify_admin_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify session token checking both Redis and in-memory token blacklists."""
    payload = verify_admin_token(token)
    if not payload:
        return None
    if await security_store.is_token_blacklisted(token):
        return None
    epoch = await security_store.get_token_epoch()
    if epoch and payload.get("iat", 0) <= epoch:
        return None
    return payload


async def revoke_token(token: str):
    """Revoke a specific admin token."""
    await security_store.blacklist_token(token)


async def revoke_all_tokens():
    """Revoke all currently issued admin tokens."""
    await security_store.set_token_epoch()


async def get_current_admin(
    request: Request,
    auth_header: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI Dependency for securing admin endpoints via Cookie or Bearer header."""
    token = None
    if auth_header and auth_header.credentials:
        token = auth_header.credentials
    elif "admin_token" in request.cookies:
        token = request.cookies.get("admin_token")
    elif "token" in request.query_params:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="احراز هویت انجام نشده است. لطفاً وارد پنل شوید.",
        )

    payload = await async_verify_admin_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="نشست شما منقضی شده یا باطل گردیده است.",
        )

    tg_id = payload.get("tg_id")
    # Verify against admin list or database user
    if tg_id not in settings.ADMIN_TELEGRAM_IDS and payload.get("role") not in (UserRole.ADMIN, UserRole.SUPER_ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="دسترسی غیرمجاز. فقط مدیران مجاز هستند.",
        )

    user_res = await db.execute(select(User).where(User.telegram_id == tg_id))
    user = user_res.scalar_one_or_none()
    if not user:
        # Create or fetch super admin placeholder if primary admin
        user = User(
            telegram_id=tg_id,
            username=payload.get("username"),
            first_name="Admin",
            role=UserRole.SUPER_ADMIN,
            referral_code=f"ADM{secrets.token_hex(4).upper()}",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    if not user.is_active or user.is_banned:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="حساب کاربری شما غیرفعال یا مسدود شده است.",
        )

    if user.role not in (UserRole.ADMIN, UserRole.SUPER_ADMIN) and tg_id not in settings.ADMIN_TELEGRAM_IDS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="دسترسی غیرمجاز. فقط مدیران مجاز هستند.",
        )

    return user

