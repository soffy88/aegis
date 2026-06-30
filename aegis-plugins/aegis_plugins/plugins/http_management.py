"""Real HTTP-management remediations.

Many remediations are "POST a management endpoint to do X" (reload config, reset a
circuit breaker, throttle traffic, …). These share one shape, so a small base class
implements the lifecycle and each concrete plugin just declares which payload key
holds the base URL and which path performs the action.

The base URL comes from the alert payload (operator-provided), mirroring the existing
real plugins (e.g. flush-cache).
"""

from __future__ import annotations

from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


class HttpManagementPlugin(AutoHealPlugin):
    """Base for remediations that hit an operator-provided management endpoint."""

    url_key: str = ""  # alert_payload key holding the base management URL
    action_path: str = ""  # path appended to the base URL for the action
    health_path: str = "/health"

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return bool(ctx.alert_payload.get(self.url_key))

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        base = ctx.alert_payload.get(self.url_key, "")
        if not base:
            return ActionResult.failed(f"{self.url_key} not in alert_payload")
        result = await ctx.http_get(base + self.action_path)
        code = result.get("status_code", 500)
        if code >= 400:
            return ActionResult.failed(f"{self.name}: endpoint returned HTTP {code}")
        return ActionResult.ok(f"{self.name}: {base + self.action_path} returned {code}")

    async def post_verify(self, ctx: AutoHealContext) -> bool:
        base = ctx.alert_payload.get(self.url_key, "")
        if not base:
            return True
        result = await ctx.http_get(base + self.health_path)
        return result.get("status_code", 0) == 200

    async def rollback(self, ctx: AutoHealContext) -> ActionResult:
        return ActionResult.skipped(f"{self.name} is not reversible")


class HealthcheckExternalPlugin(HttpManagementPlugin):
    name = "healthcheck-external"
    version = "1.0.0"
    matches_alert = "external_service_degraded"
    description = "Probe an external dependency's URL and report whether it is healthy."
    rate_limit = "10/5min"
    url_key = "probe_url"
    action_path = ""  # the probe URL itself is the health endpoint


class ReloadConfigPlugin(HttpManagementPlugin):
    name = "reload-config"
    version = "1.0.0"
    matches_alert = "config_outdated"
    description = "Reload service config via its management endpoint (SIGHUP-over-HTTP)."
    rate_limit = "5/5min"
    url_key = "reload_url"
    action_path = "/reload"


class ResetCircuitBreakerPlugin(HttpManagementPlugin):
    name = "reset-circuit-breaker"
    version = "1.0.0"
    matches_alert = "circuit_breaker_stuck"
    description = "Reset an open circuit breaker via the service management API."
    rate_limit = "5/5min"
    url_key = "breaker_url"
    action_path = "/reset"


class ThrottleTrafficPlugin(HttpManagementPlugin):
    name = "throttle-traffic"
    version = "1.0.0"
    matches_alert = "traffic_spike"
    description = "Apply rate-limiting rules via the gateway management endpoint."
    rate_limit = "5/5min"
    url_key = "ratelimit_url"
    action_path = "/throttle"


class FlushSessionsPlugin(HttpManagementPlugin):
    name = "flush-sessions"
    version = "1.0.0"
    matches_alert = "session_overflow"
    description = "Flush active sessions via the session store's management endpoint."
    rate_limit = "1/hour"
    url_key = "session_url"
    action_path = "/flush"


class ReindexSearchPlugin(HttpManagementPlugin):
    name = "reindex-search"
    version = "1.0.0"
    matches_alert = "search_index_stale"
    description = "Trigger a full search reindex via the search service endpoint."
    rate_limit = "1/hour"
    url_key = "search_url"
    action_path = "/reindex"
