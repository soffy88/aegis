"""Policy-driven closed-loop autoheal (safe by design).

Each `autoheal_policies` row binds ONE target container to a trigger
(metric/operator/threshold) and an action (restart). The loop evaluates each
enabled policy against recent metrics for that container; on breach (and past the
per-policy cooldown) it either logs the intended action (dry_run, the default) or
executes it on that specific container. There is no blanket auto-remediation — a
real restart only happens for a policy explicitly set dry_run=false.

Outcomes are written to aegis_alert_events so they appear on the autoheal
dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import asyncpg

from aegis.server.repositories.autoheal_event_repository import AutoHealEventRepository

log = logging.getLogger(__name__)

_LOOKBACK_SEC = 180


async def _trigger_value(conn: asyncpg.Connection, *, metric: str, target: str, operator: str) -> float | None:
    """Worst recent value of `metric` among series referencing `target` container.

    Matches by hostname (uptime target name) or by container name/id in tags, so it
    works for both probe_up (hostname=target) and cAdvisor metrics (id/name in tags).
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (hostname, tags) value
        FROM agent_metrics
        WHERE metric_name = $1
          AND ts > now() - ($3 * interval '1 second')
          AND ( hostname = $2
                OR tags->>'name' = $2
                OR tags->>'target' = $2
                OR (tags->>'id') LIKE '%' || $2 || '%' )
        ORDER BY hostname, tags, ts DESC
        """,
        metric,
        target,
        _LOOKBACK_SEC,
    )
    vals = [r["value"] for r in rows]
    if not vals:
        return None
    if operator in (">", ">="):
        return max(vals)
    if operator in ("<", "<="):
        return min(vals)
    return vals[0]


def _breached(value: float, operator: str, threshold: float) -> bool:
    return {
        ">=": value >= threshold,
        ">": value > threshold,
        "<=": value <= threshold,
        "<": value < threshold,
        "==": value == threshold,
    }.get(operator, False)


async def run_autoheal_policies(conn: asyncpg.Connection) -> list[dict]:
    """Evaluate all enabled policies; act on breaches past cooldown. Returns actions."""
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    policies = await conn.fetch(
        """
        SELECT id, org_id, name, target_container, trigger_metric, trigger_operator,
               trigger_threshold, action, dry_run, cooldown_seconds, docker_host,
               last_triggered_at
        FROM autoheal_policies
        WHERE enabled = TRUE
          AND (last_triggered_at IS NULL
               OR last_triggered_at <= now() - (cooldown_seconds * interval '1 second'))
        """
    )
    events = AutoHealEventRepository(conn)
    actions: list[dict] = []

    for p in policies:
        value = await _trigger_value(
            conn,
            metric=p["trigger_metric"],
            target=p["target_container"],
            operator=p["trigger_operator"],
        )
        if value is None or not _breached(value, p["trigger_operator"], p["trigger_threshold"]):
            continue

        target = p["target_container"]
        dry = p["dry_run"]
        if dry:
            reason = f"DRY-RUN: would {p['action']} {target} ({p['trigger_metric']}={value})"
            ok = True
            err = None
        else:
            from oprim import docker_container_restart  # noqa: PLC0415

            docker_host = p["docker_host"] or get_settings().docker_host
            try:
                await asyncio.to_thread(
                    docker_container_restart, container_id=target, docker_host=docker_host
                )
                ok, err = True, None
                reason = f"autoheal: restarted {target} ({p['trigger_metric']}={value})"
            except Exception as exc:  # noqa: BLE001
                ok, err = False, str(exc)[:150]
                reason = f"autoheal: FAILED to restart {target}: {err}"

        await events.insert(
            org_id=p["org_id"],
            cycle_id=uuid.uuid4(),
            severity="info" if dry else ("warning" if ok else "critical"),
            source=f"autoheal:{p['name']}",
            reason=reason,
            value=value,
        )
        await conn.execute(
            "UPDATE autoheal_policies SET last_triggered_at = now() WHERE id = $1", p["id"]
        )
        log.info("autoheal_policy_fired name=%s dry_run=%s ok=%s reason=%s", p["name"], dry, ok, reason)
        actions.append({"policy": p["name"], "dry_run": dry, "ok": ok, "reason": reason})

    return actions
