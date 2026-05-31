"""Tests for AlertEngine + WebhookDispatcher integration — C2-5. Unit tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from aegis.server.engines.alert_engine import AlertEngine
from aegis.server.schemas.alerting import AlertFiredResponse, AlertRuleResponse

_ORG = uuid.UUID("55555555-5555-5555-5555-555555555555")
_PROJ = uuid.UUID("66666666-6666-6666-6666-666666666666")
_USER = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_RULE_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _rule(**kwargs: object) -> AlertRuleResponse:
    defaults: dict = dict(
        rule_id=_RULE_ID,
        org_id=_ORG,
        project_id=_PROJ,
        name="cpu-rule",
        metric="container.cpu.percent",
        threshold_warn=70.0,
        threshold_critical=90.0,
        operator=">=",
        throttle_seconds=300,
        escalation_delay_seconds=1800,
        dedup_bucket_seconds=3600,
        enabled=True,
        created_by=_USER,
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(kwargs)
    return AlertRuleResponse.model_validate(defaults)


def _fired(**kwargs: object) -> AlertFiredResponse:
    base = dict(
        fired_id=uuid.uuid4(),
        rule_id=_RULE_ID,
        org_id=_ORG,
        project_id=_PROJ,
        dedup_key="abc123",
        severity="critical",
        current_value=95.0,
        triggered_reason="cpu >= 90",
        fired_at=_NOW,
        escalated_at=None,
        last_seen_at=_NOW,
    )
    base.update(kwargs)
    return AlertFiredResponse.model_validate(base)


class TestAlertEngineWebhookIntegration:
    async def test_evaluate_metric_fired_enqueues_webhook(self) -> None:
        """When a new alert fires, dispatcher.enqueue_event is called with alert.fired."""
        rule_repo = MagicMock()
        fired_repo = MagicMock()
        fired_repo.get_last_fired = AsyncMock(return_value=None)
        fired_row = _fired()
        fired_repo.upsert_or_update_last_seen = AsyncMock(return_value=(fired_row, True))

        mock_dispatcher = MagicMock()
        mock_dispatcher.enqueue_event = AsyncMock(return_value=1)

        engine = AlertEngine(
            rule_repo=rule_repo,
            fired_repo=fired_repo,
            webhook_dispatcher=mock_dispatcher,
        )
        result = await engine.evaluate_metric(rule=_rule(), current_value=95.0, now=_NOW)

        assert result.fired is True
        mock_dispatcher.enqueue_event.assert_awaited_once()
        call_kwargs = mock_dispatcher.enqueue_event.call_args.kwargs
        assert call_kwargs["event_type"] == "alert.fired"
        assert call_kwargs["org_id"] == _ORG
        assert str(_RULE_ID) in str(call_kwargs["payload"]["rule_id"])

    async def test_evaluate_metric_no_dispatcher_still_works(self) -> None:
        """Backward compat: no webhook_dispatcher → no enqueue, no crash."""
        rule_repo = MagicMock()
        fired_repo = MagicMock()
        fired_repo.get_last_fired = AsyncMock(return_value=None)
        fired_row = _fired()
        fired_repo.upsert_or_update_last_seen = AsyncMock(return_value=(fired_row, True))

        engine = AlertEngine(rule_repo=rule_repo, fired_repo=fired_repo)
        result = await engine.evaluate_metric(rule=_rule(), current_value=95.0, now=_NOW)
        assert result.fired is True

    async def test_evaluate_metric_dedup_does_not_enqueue(self) -> None:
        """Dedup (is_new=False) does NOT trigger webhook."""
        rule_repo = MagicMock()
        fired_repo = MagicMock()
        fired_repo.get_last_fired = AsyncMock(return_value=None)
        fired_row = _fired()
        fired_repo.upsert_or_update_last_seen = AsyncMock(return_value=(fired_row, False))

        mock_dispatcher = MagicMock()
        mock_dispatcher.enqueue_event = AsyncMock()

        engine = AlertEngine(
            rule_repo=rule_repo,
            fired_repo=fired_repo,
            webhook_dispatcher=mock_dispatcher,
        )
        result = await engine.evaluate_metric(rule=_rule(), current_value=95.0, now=_NOW)
        assert result.fired is False
        mock_dispatcher.enqueue_event.assert_not_awaited()
