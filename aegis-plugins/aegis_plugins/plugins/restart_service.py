"""restart-service — restart a Docker service reported as down."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


class RestartServicePlugin(AutoHealPlugin):
    name = "restart-service"
    version = "1.0.0"
    matches_alert = "service_down"
    description = "Restart a Docker container reported as down or unhealthy."
    rate_limit = "1/5min"

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return ctx.service.health in ("down", "unhealthy", "degraded")

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        service_name = ctx.alert_payload.get("service_name", ctx.service.name)
        try:
            await ctx.docker_restart(service_name)
            return ActionResult.ok(f"docker restart {service_name} sent")
        except Exception as exc:
            return ActionResult.failed(f"docker_restart failed: {exc}")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        health_url = ctx.alert_payload.get("health_url", "")
        if not health_url:
            return True
        result = await ctx.http_get(health_url)
        return result.get("status_code", 0) == 200

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        await ctx.alert_human("restart-service: post_verify failed, escalating to human")
        return ActionResult.escalate(to="human", detail="post_verify failed after restart")
