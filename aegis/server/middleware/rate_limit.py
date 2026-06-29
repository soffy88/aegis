"""Simple in-process IP-based rate limiter middleware.

Uses a token-bucket algorithm keyed by client IP.
Designed for auth endpoints (login/register) where brute-force
protection matters. For high-scale multi-process deployments,
replace with a Redis-backed solution (e.g. slowapi + redis).

Configuration via AegisSettings:
  AEGIS_RATE_LIMIT_AUTH_REQUESTS   – max requests per window (default: 10)
  AEGIS_RATE_LIMIT_AUTH_WINDOW_SEC – sliding window in seconds (default: 60)
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

# Paths that get rate-limited (prefix match)
_AUTH_PATHS = {
    "/api/v1/auth/login",
    "/api/v1/auth/register",
}


class AuthRateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiter for auth endpoints.

    Keyed by CF-Connecting-IP (non-spoofable Cloudflare edge IP) falling back to
    the direct peer. Bucket refills at rate = max_requests / window_sec per second.
    NOTE: state is per-process; with uvicorn --workers N the effective limit is
    N× the configured value. Move to shared Redis if strict global limits matter.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_requests: int = 10,
        window_sec: int = 60,
    ) -> None:
        super().__init__(app)
        self._max = max_requests
        self._window = window_sec
        # {ip: deque of timestamps}
        self._buckets: dict[str, collections.deque[float]] = collections.defaultdict(
            collections.deque
        )
        self._lock = asyncio.Lock()

    def _get_client_ip(self, request: Request) -> str:
        """Real client IP.

        Cloudflare is the only ingress (caddy publishes no host ports), and it
        sets CF-Connecting-IP from the real edge connection — a value clients
        cannot forge. The leftmost X-Forwarded-For hop, by contrast, IS attacker-
        controlled (Cloudflare appends the real IP after any client-supplied
        value), so keying on it let an attacker mint unlimited fresh buckets.
        Prefer CF-Connecting-IP; fall back to the direct peer.
        """
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        if request.url.path not in _AUTH_PATHS or request.method != "POST":
            return await call_next(request)

        ip = self._get_client_ip(request)
        now = time.monotonic()
        cutoff = now - self._window

        async with self._lock:
            bucket = self._buckets[ip]
            # Remove timestamps outside the sliding window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self._max:
                log.warning(
                    "rate_limit_exceeded ip=%s path=%s count=%d window=%ds",
                    ip,
                    request.url.path,
                    len(bucket),
                    self._window,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests. Please try again later."},
                    headers={"Retry-After": str(self._window)},
                )

            bucket.append(now)

        return await call_next(request)
