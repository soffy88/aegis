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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg

from aegis.server.engines.alert_engine import AlertEngine
from aegis.server.repositories.alert_fired_repository import AlertFiredRepository
from aegis.server.repositories.alert_rule_repository import AlertRuleRepository
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
    """Latest per-host value of the rule's metric, reduced to one worst-case value.

    Returns None when no recent reading exists (rule is un-evaluable this tick).
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (hostname) value
        FROM agent_metrics
        WHERE metric_name = $1 AND ts >= $2
        ORDER BY hostname, ts DESC
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


async def run_alert_evaluation(
    *,
    conn: asyncpg.Connection,
    webhook_dispatcher: WebhookDispatcher | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Evaluate all enabled rules once. Returns stats {evaluated, fired, skipped}."""
    now = now or datetime.now(UTC)
    since = now - timedelta(seconds=_METRIC_LOOKBACK_SEC)

    rule_repo = AlertRuleRepository(conn)
    fired_repo = AlertFiredRepository(conn)
    engine = AlertEngine(
        rule_repo=rule_repo,
        fired_repo=fired_repo,
        webhook_dispatcher=webhook_dispatcher,
    )

    rules = await rule_repo.list_all_enabled()
    stats = {"evaluated": 0, "fired": 0, "skipped": 0}

    for rule in rules:
        value = await _current_value_for_rule(conn, rule, since=since)
        if value is None:
            stats["skipped"] += 1
            continue
        result = await engine.evaluate_metric(rule=rule, current_value=value, now=now)
        stats["evaluated"] += 1
        if result.fired:
            stats["fired"] += 1

    if stats["fired"]:
        log.info(
            "alert_eval_tick evaluated=%d fired=%d skipped=%d",
            stats["evaluated"],
            stats["fired"],
            stats["skipped"],
        )
    return stats
