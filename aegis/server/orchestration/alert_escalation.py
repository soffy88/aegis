"""Alert escalation scheduler.

Periodically promotes warn-severity alerts that have sat unacknowledged past their
rule's escalation_delay_seconds to critical: marks them escalated (idempotent) and
emits an `alert.fired` webhook with escalated=True so notification channels fire.

Wired into the orchestration cron (see cron.py). The escalation decision itself lives
in AlertEngine.check_escalation_needed — this module is just the driver.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg

from aegis.server.engines.alert_engine import AlertEngine
from aegis.server.repositories.alert_fired_repository import AlertFiredRepository
from aegis.server.repositories.alert_rule_repository import AlertRuleRepository

if TYPE_CHECKING:
    from aegis.server.engines.webhook_dispatcher import WebhookDispatcher

log = logging.getLogger(__name__)


async def run_alert_escalation(
    *,
    conn: asyncpg.Connection,
    webhook_dispatcher: WebhookDispatcher | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Escalate every due warn alert. Returns the list of escalated fired_ids."""
    fired_repo = AlertFiredRepository(conn)
    rule_repo = AlertRuleRepository(conn)
    engine = AlertEngine(
        rule_repo=rule_repo,
        fired_repo=fired_repo,
        webhook_dispatcher=webhook_dispatcher,
    )

    pending = await fired_repo.list_pending_escalation()
    escalated: list[str] = []

    for fired in pending:
        rule = await rule_repo.get(rule_id=fired.rule_id, org_id=fired.org_id)
        if rule is None:
            continue  # rule deleted since the alert fired
        if not await engine.check_escalation_needed(fired=fired, rule=rule, now=now):
            continue

        # mark_escalated is conditional on escalated_at IS NULL, so concurrent
        # schedulers won't double-fire: only the winner gets True.
        if not await fired_repo.mark_escalated(fired_id=fired.fired_id):
            continue
        escalated.append(str(fired.fired_id))

        if webhook_dispatcher is not None:
            await webhook_dispatcher.enqueue_event(
                org_id=fired.org_id,
                event_type="alert.fired",
                payload={
                    "rule_id": str(fired.rule_id),
                    "project_id": str(fired.project_id),
                    "fired_id": str(fired.fired_id),
                    "severity": "critical",
                    "escalated": True,
                    "metric": rule.metric,
                    "escalated_from": "warn",
                },
            )

    if escalated:
        log.info("alert_escalation_done count=%d", len(escalated))
    return escalated
