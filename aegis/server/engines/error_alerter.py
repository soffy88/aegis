"""ErrorAlerter — service-layer engine for error issue → alert + webhook.

M1 scope:
- handle_new_issue: new issue → WebhookDispatcher.enqueue_event('error.new_issue')
- check_spike: current error rate vs alert rule → AlertEngine.evaluate_metric
- emit_spike_event: manual error.spike webhook (complements alert.fired from C2-2)

Not implemented in M1: scheduler / cron (M2), real-time streaming spike detection,
release regression, assignment / mute / unmute (M3+).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aegis.server.engines.webhook_dispatcher import WebhookDispatcher
from aegis.server.schemas.error_monitoring import ErrorIssueResponse

if TYPE_CHECKING:
    from aegis.server.engines.alert_engine import AlertEngine, AlertEvaluationResult
    from aegis.server.schemas.alerting import AlertRuleResponse


class ErrorAlerter:
    def __init__(
        self,
        *,
        webhook_dispatcher: WebhookDispatcher,
        alert_engine: AlertEngine | None = None,
    ) -> None:
        self.webhook_dispatcher = webhook_dispatcher
        self.alert_engine = alert_engine

    async def handle_new_issue(self, *, issue: ErrorIssueResponse) -> int:
        """Enqueue webhook for every subscription watching 'error.new_issue'.

        Returns:
            Number of delivery rows enqueued (0 if no matching subscriptions).
        """
        return await self.webhook_dispatcher.enqueue_event(
            org_id=issue.org_id,
            event_type="error.new_issue",
            payload={
                "issue_id": str(issue.issue_id),
                "project_id": str(issue.project_id),
                "fingerprint": issue.fingerprint,
                "title": issue.title,
                "exception_type": issue.exception_type,
                "exception_value": issue.exception_value,
                "event_count": issue.event_count,
                "first_seen": issue.first_seen.isoformat(),
                "last_seen": issue.last_seen.isoformat(),
                "first_release": issue.first_release,
                "state": issue.state,
            },
        )

    async def check_spike(
        self,
        *,
        rule: AlertRuleResponse,
        current_error_rate: float,
        now: datetime | None = None,
    ) -> AlertEvaluationResult | None:
        """Evaluate error rate against an alert rule via C2-2 AlertEngine.

        M1: callers compute the rate and call this manually.
        M2: a scheduler will poll and call this on an interval.

        Returns:
            AlertEvaluationResult if alert_engine is injected, else None.
        """
        if self.alert_engine is None:
            return None
        return await self.alert_engine.evaluate_metric(
            rule=rule,
            current_value=current_error_rate,
            now=now,
        )

    async def emit_spike_event(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        error_rate: float,
        window_seconds: int,
        threshold: float,
        severity: str,
    ) -> int:
        """Enqueue an 'error.spike' webhook event.

        Complements alert.fired (emitted by C2-2 AlertEngine) for subscribers
        watching 'error.spike' specifically.

        Returns:
            Number of delivery rows enqueued.
        """
        return await self.webhook_dispatcher.enqueue_event(
            org_id=org_id,
            event_type="error.spike",
            payload={
                "project_id": str(project_id),
                "error_rate": error_rate,
                "window_seconds": window_seconds,
                "threshold": threshold,
                "severity": severity,
                "detected_at": datetime.now(UTC).isoformat(),
            },
        )
