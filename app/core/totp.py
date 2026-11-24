"""RFC 6238 Time-Based One-Time Password (TOTP) & Google Authenticator Integration."""
import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
import urllib.parse
from typing import Optional
import qrcode


def generate_totp_secret() -> str:
    """Generate a standard 32-character Base32 secret (20 random bytes / 160 bits)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("utf-8").rstrip("=")


def get_totp_code(
    secret: str,
    timestamp: Optional[float] = None,
    time_step: int = 30,
    digits: int = 6,
) -> str:
    """Generate standard RFC 6238 TOTP code using HMAC-SHA1."""
    if timestamp is None:
        timestamp = time.time()

    secret_clean = secret.strip().replace(" ", "").upper()
    missing_padding = len(secret_clean) % 8
    if missing_padding:
        secret_clean += "=" * (8 - missing_padding)

    key = base64.b32decode(secret_clean, casefold=True)
    counter = int(timestamp // time_step)
    counter_bytes = struct.pack(">Q", counter)

    hmac_hash = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = hmac_hash[-1] & 0x0F
    code_int = struct.unpack(">I", hmac_hash[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10 ** digits)).zfill(digits)


def verify_totp(
    secret: str,
    code: str,
    window: int = 1,
    time_step: int = 30,
    digits: int = 6,
) -> bool:
    """Verify TOTP code against secret with a tolerance window for clock skew."""
    if not secret or not code:
        return False
    code_str = str(code).strip().replace(" ", "")
    if len(code_str) != digits or not code_str.isdigit():
        return False

    now = time.time()
    for i in range(-window, window + 1):
        t = now + (i * time_step)
        expected = get_totp_code(secret, timestamp=t, time_step=time_step, digits=digits)
        if hmac.compare_digest(code_str, expected):
            return True
    return False


def generate_otpauth_uri(
    secret: str,
    account_name: str = "admin",
    issuer_name: str = "VPN Private",
) -> str:
    """Generate otpauth:// URL for authenticator apps (Google Authenticator, Authy, etc.)."""
    label = f"{urllib.parse.quote(issuer_name)}:{urllib.parse.quote(account_name)}"
    issuer_param = urllib.parse.quote(issuer_name)
    secret_clean = secret.strip().replace(" ", "").upper()
    return f"otpauth://totp/{label}?secret={secret_clean}&issuer={issuer_param}&algorithm=SHA1&digits=6&period=30"


def generate_qr_code_base64(data: str) -> str:
    """Generate PNG QR code and return as base64 string."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")
