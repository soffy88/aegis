"""restart-container — restart a specific container by name from alert payload."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


class RestartContainerPlugin(AutoHealPlugin):
    name = "restart-container"
    version = "1.0.0"
    matches_alert = "container_unhealthy"
    description = "Restart a specific Docker container identified in the alert payload."
    rate_limit = "2/5min"

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return bool(ctx.alert_payload.get("container_name"))

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        container = ctx.alert_payload["container_name"]
        try:
            await ctx.docker_restart(container)
            return ActionResult.ok(f"container {container!r} restarted")
        except Exception as exc:
            return ActionResult.failed(f"docker_restart({container!r}) failed: {exc}")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        health_url = ctx.alert_payload.get("health_url", "")
        if not health_url:
            return True
        result = await ctx.http_get(health_url)
        return result.get("status_code", 0) == 200

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        await ctx.alert_human(
            f"restart-container: {ctx.alert_payload.get('container_name')}"
            " still unhealthy after restart",
        )
        return ActionResult.escalate(to="human", detail="container unhealthy after restart")
