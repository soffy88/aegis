"""Fingerprint-based dedup cache (Redis, short TTL)."""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis


class DedupCache:
    """Short-lived cache keyed by omodul fingerprint."""

    def __init__(self, redis_client: aioredis.Redis, ttl_sec: int = 60) -> None:
        self.redis = redis_client
        self.ttl_sec = ttl_sec

    def _key(self, fp: str) -> str:
        return f"aegis:omodul_dedup:{fp}"

    async def get(self, fp: str) -> dict[str, Any] | None:
        """Return cached result or None."""
        raw = await self.redis.get(self._key(fp))
        if raw is None:
            return None
        return json.loads(raw)  # type: ignore[no-any-return]

    async def set(self, fp: str, result: dict[str, Any]) -> None:
        """Cache a completed result."""
        await self.redis.setex(
            self._key(fp),
            self.ttl_sec,
            json.dumps(result, default=str),
        )
