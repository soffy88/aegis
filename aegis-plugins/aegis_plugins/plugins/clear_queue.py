"""clear-queue — purge a message queue via RabbitMQ management API."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


class ClearQueuePlugin(AutoHealPlugin):
    name = "clear-queue"
    version = "1.0.0"
    matches_alert = "queue_overflow"
    description = "Purge a RabbitMQ queue via the management HTTP API."
    rate_limit = "1/5min"

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return bool(
            ctx.alert_payload.get("queue_name") and ctx.alert_payload.get("rabbitmq_mgmt_url")
        )

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        base = ctx.alert_payload["rabbitmq_mgmt_url"].rstrip("/")
        vhost = ctx.alert_payload.get("vhost", "%2F")
        queue = ctx.alert_payload["queue_name"]
        result = await ctx.http_get(f"{base}/api/queues/{vhost}/{queue}/contents")
        code = result.get("status_code", 500)
        if code not in (200, 204):
            return ActionResult.failed(f"queue purge returned HTTP {code}")
        return ActionResult.ok(f"queue {queue!r} purged")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        base = ctx.alert_payload["rabbitmq_mgmt_url"].rstrip("/")
        vhost = ctx.alert_payload.get("vhost", "%2F")
        queue = ctx.alert_payload["queue_name"]
        result = await ctx.http_get(f"{base}/api/queues/{vhost}/{queue}")
        depth = (
            result.get("body", {}).get("messages", -1)
            if isinstance(result.get("body"), dict)
            else -1
        )
        return depth == 0

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        return ActionResult.failed("purged messages cannot be recovered")
