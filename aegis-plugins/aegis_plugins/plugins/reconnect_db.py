"""reconnect-db — force a service to reconnect its DB pool via restart."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin

from aegis_plugins._url_safety import UrlNotAllowed, check_url_allowed


class ReconnectDbPlugin(AutoHealPlugin):
    name = "reconnect-db"
    version = "1.0.0"
    matches_alert = "db_connection_pool_exhausted"
    description = "Restart the affected service so it rebuilds its DB connection pool."
    rate_limit = "1/10min"

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return ctx.service.health in ("degraded", "unhealthy", "down")

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        reconnect_url = ctx.alert_payload.get("reconnect_url", "")
        if reconnect_url:
            url = f"{reconnect_url}/reconnect-db"
            try:
                check_url_allowed(url)
            except UrlNotAllowed:
                pass  # fall through to the docker_restart fallback below
            else:
                result = await ctx.http_get(url)
                code = result.get("status_code", 500)
                if code < 400:
                    return ActionResult.ok(f"reconnect endpoint responded {code}")
        try:
            await ctx.docker_restart(ctx.service.name)
            return ActionResult.ok(f"restarted {ctx.service.name} to rebuild pool")
        except Exception as exc:
            return ActionResult.failed(f"restart failed: {exc}")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        health_url = ctx.alert_payload.get("health_url", "")
        if not health_url:
            return True
        try:
            check_url_allowed(health_url)
        except UrlNotAllowed:
            return False
        result = await ctx.http_get(health_url)
        return result.get("status_code", 0) == 200

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        await ctx.alert_human(f"reconnect-db failed for {ctx.service.name}, pool still exhausted")
        return ActionResult.escalate(to="human", detail="db pool still exhausted after reconnect")
