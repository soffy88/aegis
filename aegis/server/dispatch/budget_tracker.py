"""Per-user monthly budget tracker (Redis-backed)."""

from __future__ import annotations

from datetime import UTC, datetime

import redis.asyncio as aioredis

_BUDGET_TTL_SEC = 32 * 86400


class BudgetTracker:
    """Track per-user monthly omodul spend."""

    def __init__(self, redis_client: aioredis.Redis, monthly_limit_usd: float = 50.0) -> None:
        self.redis = redis_client
        self.monthly_limit_usd = monthly_limit_usd

    def _key(self, user_id: str) -> str:
        ym = datetime.now(UTC).strftime("%Y%m")
        return f"aegis:budget:{user_id}:{ym}"

    async def has_budget(self, user_id: str, requested_usd: float) -> bool:
        """Friendly pre-check for callers/UI. Not the enforcement path — see deduct()."""
        raw = await self.redis.get(self._key(user_id))
        used = float(raw) if raw else 0.0
        return (used + requested_usd) <= self.monthly_limit_usd

    async def deduct(self, user_id: str, used_usd: float) -> bool:
        """Atomically deduct cost from user's monthly budget if it still fits.

        This is the actual enforcement point: the check-and-increment runs as a
        WATCH/MULTI/EXEC optimistic transaction, so two concurrent deducts for the same
        user can't both act on a stale read and jointly exceed monthly_limit_usd — a
        conflicting concurrent write aborts the EXEC and we retry. Returns False (no
        deduction applied) if the budget is exhausted.

        Used as a *reservation* before an omodul run (reserve the call's max
        budget_usd atomically; reconcile to actual spend afterwards via settle()).
        """
        if used_usd <= 0:
            return True
        key = self._key(user_id)
        async with self.redis.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(key)
                    raw = await pipe.get(key)
                    current = float(raw) if raw else 0.0
                    if current + used_usd > self.monthly_limit_usd:
                        await pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.incrbyfloat(key, used_usd)
                    pipe.expire(key, _BUDGET_TTL_SEC)
                    await pipe.execute()
                    return True
                except aioredis.WatchError:
                    continue

    async def settle(self, user_id: str, reserved_usd: float, actual_usd: float) -> None:
        """Reconcile a prior deduct() reservation to the actual spend.

        Call after a reserved execution finishes: adjusts the counter by
        (actual - reserved). A negative delta refunds the unused reservation; a
        positive delta (actual > reserved) charges the overage even past the limit,
        since the money is already spent. On a failed run pass actual_usd=0 to
        refund the whole reservation. No-op when the reservation was 0 (deduct
        early-returned) or nothing needs adjusting.
        """
        delta = actual_usd - reserved_usd
        if reserved_usd <= 0 or delta == 0:
            return
        key = self._key(user_id)
        await self.redis.incrbyfloat(key, delta)
        await self.redis.expire(key, _BUDGET_TTL_SEC)
