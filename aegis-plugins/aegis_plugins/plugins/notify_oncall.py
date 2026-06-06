"""notify-oncall — send alert to on-call team via webhook."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


class NotifyOncallPlugin(AutoHealPlugin):
    name = "notify-oncall"
    version = "1.0.0"
    matches_alert = "*"
    description = "Send a notification to the on-call team via alert_human()."
    rate_limit = "3/hour"

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return True

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        msg = (
            f"[Aegis AutoHeal] {ctx.alert_payload.get('alert_name', 'alert')} "
            f"on {ctx.service.name} (env={ctx.org_environment.value}, "
            f"health={ctx.service.health}) — trace={ctx.trace_id}"
        )
        try:
            await ctx.alert_human(msg, channel=ctx.alert_payload.get("oncall_channel", "slack"))
            return ActionResult.ok("oncall notified")
        except Exception as exc:
            return ActionResult.failed(f"alert_human failed: {exc}")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        return True

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        return ActionResult.skipped("nothing to roll back for notification")
