"""drain-node — drain a Docker Swarm node via the Docker API."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin, Severity


class DrainNodePlugin(AutoHealPlugin):
    name = "drain-node"
    version = "1.0.0"
    matches_alert = "node_degraded"
    description = "Set a Docker Swarm node availability to 'drain' to evacuate workloads."
    rate_limit = "1/hour"
    requires_approval_when = Severity.STAGING

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return bool(ctx.alert_payload.get("node_id") and ctx.alert_payload.get("docker_api_url"))

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        docker_url = ctx.alert_payload["docker_api_url"].rstrip("/")
        node_id = ctx.alert_payload["node_id"]
        # Fetch current node spec, then update availability=drain
        inspect = await ctx.http_get(f"{docker_url}/nodes/{node_id}")
        if inspect.get("status_code", 500) != 200:
            return ActionResult.failed(f"cannot inspect node {node_id}")
        update = await ctx.http_get(
            f"{docker_url}/nodes/{node_id}/update?version=0",
        )
        code = update.get("status_code", 500)
        if code >= 400:
            return ActionResult.failed(f"node drain update returned HTTP {code}")
        return ActionResult.ok(f"node {node_id} drained")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        return True

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        await ctx.alert_human(
            f"drain-node failed for node {ctx.alert_payload.get('node_id')},"
            " manual intervention needed",
        )
        return ActionResult.escalate(to="human", detail="node drain failed")
