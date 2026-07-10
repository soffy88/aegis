"""flush-cache — flush Redis/Memcached cache on corruption or overflow."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin

from aegis_plugins._url_safety import UrlNotAllowed, check_url_allowed


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
            url = flush_url + "/flush"
            try:
                check_url_allowed(url)
            except UrlNotAllowed as exc:
                return ActionResult.failed(str(exc))
            result = await ctx.http_get(url)
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
        url = flush_url + "/health"
        try:
            check_url_allowed(url)
        except UrlNotAllowed:
            return False
        result = await ctx.http_get(url)
        return result.get("status_code", 0) == 200

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        return ActionResult.failed("cannot restore flushed cache data")
