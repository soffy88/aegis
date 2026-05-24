"""Per-user monthly budget tracker (Redis-backed)."""

from __future__ import annotations

from datetime import UTC, datetime

import redis.asyncio as aioredis


class BudgetTracker:
    """Track per-user monthly omodul spend."""

    def __init__(self, redis_client: aioredis.Redis, monthly_limit_usd: float = 50.0) -> None:
        self.redis = redis_client
        self.monthly_limit_usd = monthly_limit_usd

    def _key(self, user_id: str) -> str:
        ym = datetime.now(UTC).strftime("%Y%m")
        return f"aegis:budget:{user_id}:{ym}"

    async def has_budget(self, user_id: str, requested_usd: float) -> bool:
        """Check if user has remaining budget for this request."""
        raw = await self.redis.get(self._key(user_id))
        used = float(raw) if raw else 0.0
        return (used + requested_usd) <= self.monthly_limit_usd

    async def deduct(self, user_id: str, used_usd: float) -> None:
        """Deduct cost from user's monthly budget."""
        if used_usd <= 0:
            return
        key = self._key(user_id)
        await self.redis.incrbyfloat(key, used_usd)
        await self.redis.expire(key, 32 * 86400)
