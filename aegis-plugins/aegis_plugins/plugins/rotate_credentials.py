"""rotate-credentials — trigger credential rotation via webhook."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin, Severity


class RotateCredentialsPlugin(AutoHealPlugin):
    name = "rotate-credentials"
    version = "1.0.0"
    matches_alert = "credential_leak_detected"
    description = "Trigger credential rotation via a rotation webhook endpoint."
    rate_limit = "1/hour"
    requires_approval_when = Severity.PRODUCTION

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return bool(ctx.alert_payload.get("rotation_webhook_url"))

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        webhook = ctx.alert_payload["rotation_webhook_url"]
        result = await ctx.http_get(f"{webhook}?service={ctx.service.name}&action=rotate")
        code = result.get("status_code", 500)
        if code >= 400:
            return ActionResult.failed(f"rotation webhook returned HTTP {code}")
        return ActionResult.ok(f"credential rotation triggered for {ctx.service.name}")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        status_url = ctx.alert_payload.get("rotation_status_url", "")
        if not status_url:
            return True
        result = await ctx.http_get(status_url)
        body = result.get("body", {})
        return isinstance(body, dict) and body.get("status") == "rotated"

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        await ctx.alert_human(
            f"rotate-credentials: failed for {ctx.service.name}, manual rollback required",
        )
        return ActionResult.escalate(
            to="human", detail="credential rotation requires manual rollback"
        )
