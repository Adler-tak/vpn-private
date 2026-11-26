"""Unified security cache and session store supporting Redis with in-memory TTL fallback."""
import time
import json
import logging
from typing import Optional, Dict, Any, Tuple, List

logger = logging.getLogger(__name__)


class SecurityStore:
    """Manages 2FA OTP sessions, IP rate limits, and token blacklisting.
    
    If Redis is available and configured, uses Redis keys with native TTL.
    Otherwise, uses thread-safe memory storage with automatic TTL expiry.
    """

    def __init__(self):
        self._redis = None
        self._redis_checked = False
        # Memory storage fallback
        self._otp_sessions: Dict[str, Dict[str, Any]] = {}
        self._failed_attempts: Dict[str, List[float]] = {}
        self._blocked_ips: Dict[str, float] = {}
        self._blacklisted_tokens: Dict[str, float] = {}
        self._token_epoch: int = 0

    async def _get_redis(self):
        """Lazy-load Redis client if configured."""
        if not self._redis_checked:
            self._redis_checked = True
            try:
                from app.core.config import settings
                redis_url = getattr(settings, "REDIS_URL", None)
                if redis_url:
                    import redis.asyncio as aioredis
                    client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
                    await client.ping()
                    self._redis = client
                    logger.info("SecurityStore connected to Redis successfully.")
            except Exception as e:
                logger.warning(f"SecurityStore Redis connection unavailable ({e}); using memory store.")
                self._redis = None
        return self._redis

    # -------------------------------------------------------------
    # 2FA OTP Sessions
    # -------------------------------------------------------------
    async def set_otp_session(self, session_token: str, data: Dict[str, Any], ttl_seconds: int = 120):
        r = await self._get_redis()
        if r:
            try:
                await r.setex(f"otp:{session_token}", ttl_seconds, json.dumps(data))
                return
            except Exception as e:
                logger.warning(f"Redis set_otp_session failed: {e}")
        now = time.time()
        data["expires_at"] = now + ttl_seconds
        self._otp_sessions[session_token] = data

    async def get_otp_session(self, session_token: str) -> Optional[Dict[str, Any]]:
        r = await self._get_redis()
        if r:
            try:
                raw = await r.get(f"otp:{session_token}")
                return json.loads(raw) if raw else None
            except Exception as e:
                logger.warning(f"Redis get_otp_session failed: {e}")
        now = time.time()
        s = self._otp_sessions.get(session_token)
        if not s:
            return None
        if now > s.get("expires_at", 0):
            del self._otp_sessions[session_token]
            return None
        return s

    async def delete_otp_session(self, session_token: str):
        r = await self._get_redis()
        if r:
            try:
                await r.delete(f"otp:{session_token}")
            except Exception:
                pass
        self._otp_sessions.pop(session_token, None)

    # -------------------------------------------------------------
    # IP Brute-Force Rate Limiting
    # -------------------------------------------------------------
    async def check_ip_blocked(self, client_ip: str) -> Tuple[bool, int]:
        """Check if an IP is temporarily locked out. Returns (is_blocked, remaining_seconds)."""
        now = time.time()
        r = await self._get_redis()
        if r:
            try:
                ttl = await r.ttl(f"block_ip:{client_ip}")
                if ttl and ttl > 0:
                    return True, ttl
                return False, 0
            except Exception:
                pass

        if client_ip in self._blocked_ips:
            if now < self._blocked_ips[client_ip]:
                return True, int(self._blocked_ips[client_ip] - now)
            del self._blocked_ips[client_ip]
        return False, 0

    async def record_login_failure(
        self,
        client_ip: str,
        max_attempts: int = 5,
        window_seconds: int = 900,
        lockout_seconds: int = 900,
    ) -> Tuple[int, bool]:
        """Record a failed login. Returns (current_failures, was_blocked)."""
        now = time.time()
        r = await self._get_redis()
        if r:
            try:
                key = f"fail_ip:{client_ip}"
                count = await r.incr(key)
                if count == 1:
                    await r.expire(key, window_seconds)
                if count >= max_attempts:
                    await r.setex(f"block_ip:{client_ip}", lockout_seconds, "1")
                    await r.delete(key)
                    return count, True
                return count, False
            except Exception:
                pass

        recent = [t for t in self._failed_attempts.get(client_ip, []) if now - t < window_seconds]
        recent.append(now)
        self._failed_attempts[client_ip] = recent
        if len(recent) >= max_attempts:
            self._blocked_ips[client_ip] = now + lockout_seconds
            self._failed_attempts.pop(client_ip, None)
            return len(recent), True
        return len(recent), False

    async def reset_login_failures(self, client_ip: str):
        r = await self._get_redis()
        if r:
            try:
                await r.delete(f"fail_ip:{client_ip}")
                await r.delete(f"block_ip:{client_ip}")
            except Exception:
                pass
        self._failed_attempts.pop(client_ip, None)
        self._blocked_ips.pop(client_ip, None)

    def clear_all_blocks(self):
        """Synchronously clear all in-memory rate limits, failed attempts, and blocked IPs."""
        self._blocked_ips.clear()
        self._failed_attempts.clear()

    async def clear_all_blocks_async(self):
        """Asynchronously clear all rate limits and blocked IPs from Redis and memory."""
        self.clear_all_blocks()
        r = await self._get_redis()
        if r:
            try:
                async for key in r.scan_iter("block_ip:*"):
                    await r.delete(key)
                async for key in r.scan_iter("fail_ip:*"):
                    await r.delete(key)
            except Exception as e:
                logger.warning(f"Redis clear_all_blocks_async failed: {e}")

    @property
    def failed_attempts(self) -> Dict[str, List[float]]:
        """Expose failed attempts dict to prevent AttributeError."""
        return self._failed_attempts

    @property
    def blocked_ips(self) -> Dict[str, float]:
        """Expose blocked IPs dict to prevent AttributeError."""
        return self._blocked_ips

    # -------------------------------------------------------------
    # Token Revocation & Epoch Management
    # -------------------------------------------------------------
    async def blacklist_token(self, token: str, ttl_seconds: int = 86400 * 7):
        """Revoke a specific token until its natural expiration."""
        r = await self._get_redis()
        if r:
            try:
                await r.setex(f"bl_token:{token}", ttl_seconds, "1")
                return
            except Exception:
                pass
        now = time.time()
        self._blacklisted_tokens[token] = now + ttl_seconds
        # Clean up stale memory entries occasionally
        if len(self._blacklisted_tokens) > 500:
            self._blacklisted_tokens = {k: v for k, v in self._blacklisted_tokens.items() if v > now}

    async def is_token_blacklisted(self, token: str) -> bool:
        r = await self._get_redis()
        if r:
            try:
                exists = await r.exists(f"bl_token:{token}")
                if exists:
                    return True
            except Exception:
                pass
        now = time.time()
        exp = self._blacklisted_tokens.get(token)
        if exp is not None:
            if now < exp:
                return True
            del self._blacklisted_tokens[token]
        return False

    async def set_token_epoch(self, epoch: Optional[int] = None):
        """Invalidate all tokens issued before epoch."""
        ep = epoch if epoch is not None else int(time.time())
        r = await self._get_redis()
        if r:
            try:
                await r.set("token_epoch", str(ep))
            except Exception:
                pass
        self._token_epoch = ep

    async def get_token_epoch(self) -> int:
        r = await self._get_redis()
        if r:
            try:
                raw = await r.get("token_epoch")
                if raw:
                    return int(raw)
            except Exception:
                pass
        return self._token_epoch


security_store = SecurityStore()
