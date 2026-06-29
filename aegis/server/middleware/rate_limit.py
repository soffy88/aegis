"""IP-based rate limiter middleware for auth-adjacent endpoints.

Primary backend is a Redis fixed-window counter so the limit is shared across
uvicorn workers / replicas. If Redis is unreachable the middleware falls back to a
per-process token bucket (fail-open on the shared limit, never fail the request).

Protected (brute-force surface):
  POST /api/v1/auth/login, /register, /refresh   – credential / token brute force
  ANY  /api/v1/invites/<token>[/accept]          – invite-token enumeration

Configuration via AegisSettings:
  AEGIS_RATE_LIMIT_AUTH_REQUESTS   – max requests per window (default: 10)
  AEGIS_RATE_LIMIT_AUTH_WINDOW_SEC – window in seconds (default: 60)
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

log = logging.getLogger(__name__)

# Exact paths limited on POST only, each keyed under its own bucket group.
_AUTH_PATHS = {
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/refresh",
}
# Token-bearing invite paths (verify GET + accept POST). Matched by prefix and
# collapsed into one bucket group so a rotating token can't mint fresh buckets.
_INVITE_PREFIX = "/api/v1/invites/"


def _limit_group(path: str, method: str) -> str | None:
    """Return the bucket group for a limited request, or None to skip."""
    if method == "POST" and path in _AUTH_PATHS:
        return path
    if path.startswith(_INVITE_PREFIX):
        return "invites"
    return None


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window (Redis) / token-bucket (in-process fallback) rate limiter.

    Keyed by CF-Connecting-IP (non-spoofable Cloudflare edge IP) falling back to
    the direct peer.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_requests: int = 10,
        window_sec: int = 60,
        redis_url: str | None = None,
    ) -> None:
        super().__init__(app)
        self._max = max_requests
        self._window = window_sec
        self._redis_url = redis_url
        self._redis: Any | None = None
        self._redis_disabled = redis_url is None
        # In-process fallback: {group:ip: deque of timestamps}
        self._buckets: dict[str, collections.deque[float]] = collections.defaultdict(
            collections.deque
        )
        self._lock = asyncio.Lock()

    def _get_client_ip(self, request: Request) -> str:
        """Real client IP.

        Cloudflare is the only ingress (caddy publishes no host ports), and it
        sets CF-Connecting-IP from the real edge connection — a value clients
        cannot forge. The leftmost X-Forwarded-For hop, by contrast, IS attacker-
        controlled, so keying on it would let an attacker mint unlimited buckets.
        Prefer CF-Connecting-IP; fall back to the direct peer.
        """
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        return request.client.host if request.client else "unknown"

    async def _get_redis(self) -> Any | None:
        if self._redis_disabled:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as aioredis  # noqa: PLC0415

                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            except Exception as exc:  # pragma: no cover - import/url errors
                log.warning("rate_limit_redis_init_failed err=%s — using in-process", exc)
                self._redis_disabled = True
                return None
        return self._redis

    async def _over_limit_redis(self, redis: Any, group: str, ip: str) -> bool:
        """Fixed-window counter shared across workers. Raises on Redis error."""
        bucket = int(time.time()) // self._window
        key = f"aegis:rl:{group}:{ip}:{bucket}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, self._window)
        return count > self._max

    async def _over_limit_local(self, group: str, ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        async with self._lock:
            bucket = self._buckets[f"{group}:{ip}"]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return True
            bucket.append(now)
            return False

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        group = _limit_group(request.url.path, request.method)
        if group is None:
            return await call_next(request)

        ip = self._get_client_ip(request)

        over = False
        redis = await self._get_redis()
        if redis is not None:
            try:
                over = await self._over_limit_redis(redis, group, ip)
            except Exception as exc:
                log.warning("rate_limit_redis_failed err=%s — falling back in-process", exc)
                self._redis = None  # force reconnect next time
                over = await self._over_limit_local(group, ip)
        else:
            over = await self._over_limit_local(group, ip)

        if over:
            log.warning(
                "rate_limit_exceeded ip=%s group=%s path=%s window=%ds",
                ip,
                group,
                request.url.path,
                self._window,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
                headers={"Retry-After": str(self._window)},
            )

        return await call_next(request)
