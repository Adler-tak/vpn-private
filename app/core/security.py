"""Security, cryptographic utilities, and panel credential encryption."""
import logging
import base64
import hashlib
import hmac
import os
import secrets
import uuid
from typing import Optional
from cryptography.fernet import Fernet
from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_INSECURE_SECRET = "vpn-private-super-secure-production-secret-change-me-32b"
DEFAULT_INSECURE_FERNET = "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcw=="


def generate_fernet_key() -> str:
    """Generate a fresh URL-safe base64-encoded 32-byte key for Fernet encryption."""
    return Fernet.generate_key().decode("utf-8")


def generate_secret_key(length_bytes: int = 32) -> str:
    """Generate a cryptographically strong random secret key."""
    return secrets.token_hex(length_bytes)


def validate_security_keys():
    """Warn or alert if default insecure cryptographic keys are in active use."""
    if settings.SECRET_KEY == DEFAULT_INSECURE_SECRET:
        logger.warning(
            "CRITICAL SECURITY WARNING: Default SECRET_KEY is in use! "
            "Please generate a random SECRET_KEY in your .env file immediately."
        )
    if settings.ENCRYPTION_KEY == DEFAULT_INSECURE_FERNET:
        logger.warning(
            "CRITICAL SECURITY WARNING: Default test ENCRYPTION_KEY is in use! "
            "Generate a dedicated Fernet key with Fernet.generate_key() for production."
        )


def get_fernet_cipher() -> Fernet:
    """Return Fernet cipher using configured key or derived key."""
    key = settings.ENCRYPTION_KEY
    if not key or len(key) < 32:
        # Fallback to key derived from SECRET_KEY
        derived = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(derived).decode()
    else:
        # Ensure it is valid base64 32 bytes
        try:
            Fernet(key.encode() if isinstance(key, str) else key)
        except Exception:
            derived = hashlib.sha256(key.encode()).digest()
            key = base64.urlsafe_b64encode(derived).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_credential(plain_text: str) -> str:
    """Encrypt panel credentials or sensitive tokens at rest."""
    if not plain_text:
        return ""
    cipher = get_fernet_cipher()
    return cipher.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_credential(cipher_text: str) -> str:
    """Decrypt panel credentials."""
    if not cipher_text:
        return ""
    try:
        cipher = get_fernet_cipher()
        return cipher.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        raise ValueError(f"Failed to decrypt credential: {exc}") from exc


def generate_secure_uuid() -> str:
    """Generate RFC 4122 v4 UUID for Xray clients."""
    return str(uuid.uuid4())


def generate_sub_id(length: int = 16) -> str:
    """Generate a high-entropy URL-safe subscription identifier."""
    return secrets.token_hex(length // 2)


def hash_token(token: str) -> str:
    """Hash sensitive tokens for indexing/matching without storing raw token."""
    return hashlib.sha256(token.encode()).hexdigest()


def validate_telegram_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """
    Validate Telegram Mini App initData per Telegram's official specification:
    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    import urllib.parse
    import json
    import time
    if not init_data or not bot_token:
        return None
    try:
        # Strip optional "tma " prefix if present in Authorization header
        raw_query = init_data.strip()
        if raw_query.lower().startswith("tma "):
            raw_query = raw_query[4:].strip()
        elif raw_query.lower().startswith("bearer "):
            raw_query = raw_query[7:].strip()

        parsed_data = dict(urllib.parse.parse_qsl(raw_query, keep_blank_values=True))
        received_hash = parsed_data.pop("hash", None)
        if not received_hash:
            return None

        # Build data_check_string from sorted key-value pairs
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        # secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        
        # expected_hash = HMAC_SHA256(key=secret_key, msg=data_check_string)
        expected_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(received_hash, expected_hash):
            return None

        # Optional auth_date freshness check (within 48 hours)
        auth_date = int(parsed_data.get("auth_date", 0))
        if auth_date > 0:
            now_ts = int(time.time())
            if now_ts - auth_date > 86400 * 2:
                logger.warning("Telegram initData expired (older than 48h)")
                return None

        if "user" in parsed_data and isinstance(parsed_data["user"], str):
            parsed_data["user"] = json.loads(parsed_data["user"])

        return parsed_data
    except Exception as exc:
        logger.warning(f"Error validating telegram initData: {exc}")
        return None


def create_access_token(*args, **kwargs):
    """Convenience alias for admin JWT creation."""
    from app.api.auth import create_admin_token
    return create_admin_token(*args, **kwargs)


# Re-export TOTP utilities for convenience
from app.core.totp import (
    generate_totp_secret,
    get_totp_code,
    verify_totp,
    generate_otpauth_uri,
    generate_qr_code_base64,
)

