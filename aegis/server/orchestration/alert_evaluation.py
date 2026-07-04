"""Alert rule evaluation scheduler (audit P0 #2).

Periodically evaluates every enabled alert rule against the most recent scraped /
pushed metrics in `agent_metrics`, firing alerts (and enqueuing `alert.fired`
webhooks) via AlertEngine.evaluate_metric.

Before this module, AlertEngine.evaluate_metric had no periodic caller, so
user-configured threshold rules never auto-fired (error_alerter.py noted "M2: a
scheduler will poll"). Wired into the orchestration cron (see cron.py); the
threshold/throttle/dedup/fire decision lives in AlertEngine — this is the driver.

Metric mapping: `alert_rules.metric` is a free-text metric name and `agent_metrics`
has no project linkage, so a rule is evaluated against the latest per-host value of
that metric_name within a lookback window. The engine dedups host-agnostically
(entity = project:metric), so a single representative value is fed: the worst host
for the operator direction (max for >/>=, min for </<=, most-recent for ==). This
gives "fire if any host breaches" semantics. Project-scoped metrics are a known
limitation of the current agent_metrics schema, not of this loop.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg

from aegis.server.engines.alert_engine import AlertEngine
from aegis.server.repositories.alert_fired_repository import AlertFiredRepository
from aegis.server.repositories.alert_rule_repository import AlertRuleRepository
from aegis.server.repositories.autoheal_event_repository import AutoHealEventRepository
from aegis.server.schemas.alerting import AlertRuleResponse

if TYPE_CHECKING:
    from aegis.server.engines.webhook_dispatcher import WebhookDispatcher

log = logging.getLogger(__name__)

# How far back a metric reading may be and still count as "current". Matches the
# evaluation cadence headroom; older data is treated as "no signal" (skip).
_METRIC_LOOKBACK_SEC = 300


async def _current_value_for_rule(
    conn: asyncpg.Connection,
    rule: AlertRuleResponse,
    *,
    since: datetime,
) -> float | None:
    """Latest value of each distinct series for the rule's metric, reduced to one
    worst-case value.

    A "series" is (hostname, tags): for agent-push metrics that's one row per host;
    for Prometheus-style metrics (e.g. cAdvisor, where every container shares
    hostname and is distinguished by its label set in `tags`) it's one row per
    container. Aggregating per-series — not per-host — lets "any container over
    threshold" rules fire correctly. Returns None when no recent reading exists.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (hostname, tags) value
        FROM agent_metrics
        WHERE metric_name = $1 AND ts >= $2
        ORDER BY hostname, tags, ts DESC
        """,
        rule.metric,
        since,
    )
    values = [r["value"] for r in rows]
    if not values:
        return None
    # Pick the value most likely to breach for the operator direction.
    if rule.operator in (">", ">="):
        return max(values)
    if rule.operator in ("<", "<="):
        return min(values)
    return values[0]  # '==' : any host's latest reading


async def _worst_series_for_rule(
    conn: asyncpg.Connection,
    rule: AlertRuleResponse,
    *,
    since: datetime,
) -> tuple[float, str | None] | None:
    """Like _current_value_for_rule but also returns the worst series' hostname, so
    §3.2 host-down suppression can attribute the fire to its parent host.

    Returns (value, hostname) of the breach-most-likely series, or None when no
    recent reading exists. hostname may be None for hostless metrics."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (hostname, tags) value, hostname
        FROM agent_metrics
        WHERE metric_name = $1 AND ts >= $2
        ORDER BY hostname, tags, ts DESC
        """,
        rule.metric,
        since,
    )
    if not rows:
        return None
    pairs = [(r["value"], r.get("hostname")) for r in rows]
    if rule.operator in (">", ">="):
        return max(pairs, key=lambda x: x[0])
    if rule.operator in ("<", "<="):
        return min(pairs, key=lambda x: x[0])
    return pairs[0]  # '==' : any series


async def _host_liveness(
    conn: asyncpg.Connection, *, since: datetime, metric: str
) -> dict[str, str]:
    """每 host 最新存活值 → {hostname: 'up'|'down'}(value>0=up)。用于 §3.2 父级下线抑制。

    metric 空或无数据 → {}(不抑制任何告警,安全默认)。"""
    if not metric:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (hostname) hostname, value
        FROM agent_metrics
        WHERE metric_name = $1 AND ts >= $2
        ORDER BY hostname, ts DESC
        """,
        metric,
        since,
    )
    return {
        r.get("hostname"): ("up" if r["value"] > 0 else "down")
        for r in rows
        if r.get("hostname") is not None
    }


async def run_alert_evaluation(
    *,
    conn: asyncpg.Connection,
    webhook_dispatcher: WebhookDispatcher | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Evaluate all enabled rules once. Returns stats {evaluated, fired, skipped, suppressed}."""
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    now = now or datetime.now(UTC)
    since = now - timedelta(seconds=_METRIC_LOOKBACK_SEC)
    cfg = get_settings()

    rule_repo = AlertRuleRepository(conn)
    fired_repo = AlertFiredRepository(conn)
    autoheal_repo = AutoHealEventRepository(conn)
    engine = AlertEngine(
        rule_repo=rule_repo,
        fired_repo=fired_repo,
        webhook_dispatcher=webhook_dispatcher,
    )

    rules = await rule_repo.list_all_enabled()
    stats = {"evaluated": 0, "fired": 0, "skipped": 0, "suppressed": 0}

    # §3.2 每 host 存活映射(父级下线判定源)。禁用或无存活指标 → 空 → 不抑制任何告警。
    parent_states: dict[str, str] = {}
    if cfg.alert_suppress_enabled:
        parent_states = await _host_liveness(
            conn, since=since, metric=cfg.alert_host_liveness_metric
        )

    for rule in rules:
        series = await _worst_series_for_rule(conn, rule, since=since)
        if series is None:
            stats["skipped"] += 1
            continue
        value, host = series
        # §3.2 host-down 抑制:该告警父级(host)下线 → 抑制,不评估不触发(只留 host-down 根因,
        # 消除单宿主故障引发的告警风暴)。AlertEngine.evaluate_metric 内部即 fire+enqueue,
        # 故必须在其之前拦截。
        if host is not None and parent_states:
            from oskill.alert_suppress import alert_suppress  # noqa: PLC0415

            verdict = alert_suppress(alert={"parent": host}, parent_states=parent_states)
            if verdict.suppressed:
                stats["suppressed"] += 1
                log.info(
                    "alert_suppressed_host_down rule=%s host=%s reason=%s",
                    rule.name,
                    host,
                    verdict.reason,
                )
                continue
        result = await engine.evaluate_metric(rule=rule, current_value=value, now=now)
        stats["evaluated"] += 1
        if result.fired:
            stats["fired"] += 1
            # Populate aegis_alert_events so the autoheal dashboard/stats reflect
            # real activity (the table previously had no writer). This records the
            # signal; automatic remediation against it is a separate, policy-gated
            # step (see STATUS Needs-Human: autoheal policy model).
            await autoheal_repo.insert(
                org_id=rule.org_id,
                cycle_id=uuid.uuid4(),
                severity=result.severity,
                source=f"alert_rule:{rule.name}",
                reason=result.reason,
                value=value,
            )

    if stats["fired"] or stats["suppressed"]:
        log.info(
            "alert_eval_tick evaluated=%d fired=%d skipped=%d suppressed=%d",
            stats["evaluated"],
            stats["fired"],
            stats["skipped"],
            stats["suppressed"],
        )
    return stats
