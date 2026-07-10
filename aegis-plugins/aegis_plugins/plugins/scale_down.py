"""scale-down — reduce replica count to relieve memory/CPU pressure."""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin, Severity

from aegis_plugins._url_safety import UrlNotAllowed, check_url_allowed


class ScaleDownPlugin(AutoHealPlugin):
    name = "scale-down"
    version = "1.0.0"
    matches_alert = "high_memory_pressure"
    description = "Scale a Docker service down by one replica to relieve resource pressure."
    rate_limit = "1/10min"
    requires_approval_when = Severity.PRODUCTION
    blocklist_when = ["deploy_in_progress", "scheduled_maintenance"]

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        current = ctx.alert_payload.get("current_replicas", 0)
        return int(current) > 1

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        docker_url = ctx.alert_payload.get("docker_api_url", "").rstrip("/")
        service_id = ctx.alert_payload.get("service_id", ctx.service.name)
        current = int(ctx.alert_payload.get("current_replicas", 2))
        target = max(1, current - 1)

        if docker_url:
            url = f"{docker_url}/services/{service_id}/update?replicas={target}"
            try:
                check_url_allowed(url)
            except UrlNotAllowed as exc:
                return ActionResult.failed(str(exc))
            result = await ctx.http_get(url)
            code = result.get("status_code", 500)
            if code >= 400:
                return ActionResult.failed(f"scale-down API returned HTTP {code}")
            return ActionResult.ok(f"scaled {service_id} from {current} to {target} replicas")
        return ActionResult.failed("docker_api_url not in alert_payload")

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
        # Restore original replica count
        docker_url = ctx.alert_payload.get("docker_api_url", "").rstrip("/")
        service_id = ctx.alert_payload.get("service_id", ctx.service.name)
        original = int(ctx.alert_payload.get("current_replicas", 2))
        if docker_url:
            url = f"{docker_url}/services/{service_id}/update?replicas={original}"
            try:
                check_url_allowed(url)
            except UrlNotAllowed as exc:
                return ActionResult.failed(str(exc))
            await ctx.http_get(url)
            return ActionResult.ok(f"restored {service_id} to {original} replicas")
        return ActionResult.failed("cannot restore replicas: docker_api_url missing")
