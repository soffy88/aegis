"""flush-cache — flush Redis/Memcached cache on corruption or overflow."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


class FlushCachePlugin(AutoHealPlugin):
    name = "flush-cache"
    version = "1.0.0"
    matches_alert = "cache_corruption"
    description = "Flush the cache layer (Redis FLUSHDB) to clear corrupt entries."
    rate_limit = "1/hour"

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return True

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        flush_url = ctx.alert_payload.get("cache_flush_url", "")
        if flush_url:
            result = await ctx.http_get(flush_url + "/flush")
            if result.get("status_code", 500) >= 400:
                return ActionResult.failed(
                    f"cache flush endpoint returned {result.get('status_code')}"
                )
            return ActionResult.ok("cache flushed via HTTP endpoint")
        return ActionResult.failed("cache_flush_url not in alert_payload")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        flush_url = ctx.alert_payload.get("cache_flush_url", "")
        if not flush_url:
            return True
        result = await ctx.http_get(flush_url + "/health")
        return result.get("status_code", 0) == 200

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        return ActionResult.failed("cannot restore flushed cache data")
