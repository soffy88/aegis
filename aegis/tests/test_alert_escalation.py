"""Tests for the alert escalation scheduler (orchestration.alert_escalation)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.orchestration.alert_escalation import run_alert_escalation
from aegis.server.schemas.alerting import AlertFiredResponse, AlertRuleResponse

_ORG = uuid.uuid4()
_PROJECT = uuid.uuid4()
_RULE = uuid.uuid4()
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _fired(*, severity: str = "warn", escalated: bool = False, age_sec: int = 3600) -> AlertFiredResponse:
    return AlertFiredResponse(
        fired_id=uuid.uuid4(),
        rule_id=_RULE,
        org_id=_ORG,
        project_id=_PROJECT,
        dedup_key="k",
        severity=severity,  # type: ignore[arg-type]
        current_value=90.0,
        triggered_reason="r",
        fired_at=_NOW - timedelta(seconds=age_sec),
        escalated_at=None if not escalated else _NOW,
        last_seen_at=_NOW,
    )


def _rule(*, escalation_delay_seconds: int = 1800) -> AlertRuleResponse:
    return AlertRuleResponse(
        rule_id=_RULE,
        org_id=_ORG,
        project_id=_PROJECT,
        name="cpu",
        metric="cpu",
        threshold_warn=80.0,
        threshold_critical=95.0,
        operator=">=",
        throttle_seconds=300,
        escalation_delay_seconds=escalation_delay_seconds,
        dedup_bucket_seconds=3600,
        enabled=True,
        created_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _patch_repos(pending: list[AlertFiredResponse], rule: AlertRuleResponse | None, *, marked: bool = True):
    fired_repo = MagicMock()
    fired_repo.list_pending_escalation = AsyncMock(return_value=pending)
    fired_repo.mark_escalated = AsyncMock(return_value=marked)
    rule_repo = MagicMock()
    rule_repo.get = AsyncMock(return_value=rule)
    return (
        patch("aegis.server.orchestration.alert_escalation.AlertFiredRepository", return_value=fired_repo),
        patch("aegis.server.orchestration.alert_escalation.AlertRuleRepository", return_value=rule_repo),
        fired_repo,
        rule_repo,
    )


@pytest.mark.asyncio
async def test_escalates_due_warn_alert_and_enqueues_webhook() -> None:
    p_fired, p_rule, fired_repo, _ = _patch_repos([_fired()], _rule())
    dispatcher = MagicMock()
    dispatcher.enqueue_event = AsyncMock(return_value=1)
    with p_fired, p_rule:
        escalated = await run_alert_escalation(
            conn=MagicMock(), webhook_dispatcher=dispatcher, now=_NOW
        )
    assert len(escalated) == 1
    fired_repo.mark_escalated.assert_awaited_once()
    args = dispatcher.enqueue_event.await_args.kwargs
    assert args["event_type"] == "alert.fired"
    assert args["payload"]["severity"] == "critical"
    assert args["payload"]["escalated"] is True


@pytest.mark.asyncio
async def test_skips_when_not_yet_past_delay() -> None:
    # fired 60s ago, delay 1800s → not due
    p_fired, p_rule, fired_repo, _ = _patch_repos([_fired(age_sec=60)], _rule())
    dispatcher = MagicMock()
    dispatcher.enqueue_event = AsyncMock()
    with p_fired, p_rule:
        escalated = await run_alert_escalation(
            conn=MagicMock(), webhook_dispatcher=dispatcher, now=_NOW
        )
    assert escalated == []
    fired_repo.mark_escalated.assert_not_called()
    dispatcher.enqueue_event.assert_not_called()


@pytest.mark.asyncio
async def test_skips_when_rule_deleted() -> None:
    p_fired, p_rule, fired_repo, _ = _patch_repos([_fired()], None)
    with p_fired, p_rule:
        escalated = await run_alert_escalation(conn=MagicMock(), now=_NOW)
    assert escalated == []
    fired_repo.mark_escalated.assert_not_called()


@pytest.mark.asyncio
async def test_no_double_fire_when_mark_loses_race() -> None:
    """mark_escalated returns False (another worker won) → no webhook."""
    p_fired, p_rule, _, _ = _patch_repos([_fired()], _rule(), marked=False)
    dispatcher = MagicMock()
    dispatcher.enqueue_event = AsyncMock()
    with p_fired, p_rule:
        escalated = await run_alert_escalation(
            conn=MagicMock(), webhook_dispatcher=dispatcher, now=_NOW
        )
    assert escalated == []
    dispatcher.enqueue_event.assert_not_called()
