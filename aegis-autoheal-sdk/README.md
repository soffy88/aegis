# aegis-autoheal-sdk

Stable contract for Aegis **AutoHeal** plugins — the small set of base classes and
types a plugin codes against, decoupled from the aegis backend internals.

## What it provides

| Symbol | Purpose |
|---|---|
| `Severity` | Deployment environment (`DEV` / `STAGING` / `PRODUCTION`) — drives approval gating |
| `ActionResultStatus` | `OK` / `FAILED` / `ESCALATE` / `SKIPPED` |
| `ActionResult` | Action outcome; build via `ActionResult.ok/failed/escalate/skipped(...)` |
| `ServiceInfo` | Read-only view of the target service (`name` / `health` / `version`) |
| `AutoHealContext` | Capabilities a plugin may call (`docker_restart`, `http_get`, `get_secret`, `emit_trail_event`, …); the host provides a concrete implementation |
| `AutoHealPlugin` | Base class; lifecycle `pre_check → execute → post_verify → rollback` |

## Plugin example

```python
from aegis_autoheal_sdk import ActionResult, AutoHealContext, AutoHealPlugin


class RestartContainerPlugin(AutoHealPlugin):
    name = "restart-container"
    version = "1.0.0"
    matches_alert = "container_unhealthy"
    description = "Restart a container named in the alert payload."

    async def pre_check(self, ctx: AutoHealContext) -> bool:
        return bool(ctx.alert_payload.get("container_name"))

    async def execute(self, ctx: AutoHealContext) -> ActionResult:
        name = ctx.alert_payload["container_name"]
        await ctx.docker_restart(name)
        return ActionResult.ok(f"restarted {name!r}")
```

Consumed by `aegis-plugins` (the built-in plugin pack) and by the aegis backend's
AutoHeal engine, which supplies the concrete `AutoHealContext` / `ServiceInfo`.

Apache-2.0.
