"""Tests for ErrorAlerter — pure unit tests (mock dispatcher + alert_engine)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from aegis.server.engines.error_alerter import ErrorAlerter
from aegis.server.schemas.error_monitoring import ErrorIssueResponse

_ORG = uuid.UUID("fc050001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("fc050002-0000-0000-0000-000000000000")


def _now() -> datetime:
    return datetime(2026, 6, 1, tzinfo=UTC)


def _fake_issue(**kwargs: object) -> ErrorIssueResponse:
    defaults = dict(
        issue_id=uuid.uuid4(),
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint="a" * 64,
        exception_type="TypeError",
        exception_value="bad arg",
        title="TypeError: bad arg",
        event_count=1,
        user_count=0,
        first_seen=_now(),
        last_seen=_now(),
        state="unresolved",
        first_release=None,
        last_release="v1.0.0",
        created_at=_now(),
        updated_at=_now(),
    )
    defaults.update(kwargs)
    return ErrorIssueResponse(**defaults)


@pytest.fixture
def mock_dispatcher() -> AsyncMock:
    d = AsyncMock()
    d.enqueue_event.return_value = 1
    return d


@pytest.fixture
def mock_alert_engine() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def alerter(mock_dispatcher: AsyncMock) -> ErrorAlerter:
    return ErrorAlerter(webhook_dispatcher=mock_dispatcher)


@pytest.fixture
def alerter_with_engine(mock_dispatcher: AsyncMock, mock_alert_engine: AsyncMock) -> ErrorAlerter:
    return ErrorAlerter(webhook_dispatcher=mock_dispatcher, alert_engine=mock_alert_engine)


# ---------------------------------------------------------------------------
# handle_new_issue
# ---------------------------------------------------------------------------


async def test_handle_new_issue_enqueues_webhook(
    alerter: ErrorAlerter, mock_dispatcher: AsyncMock
) -> None:
    issue = _fake_issue()
    count = await alerter.handle_new_issue(issue=issue)
    assert count == 1
    mock_dispatcher.enqueue_event.assert_awaited_once()
    call_kwargs = mock_dispatcher.enqueue_event.call_args.kwargs
    assert call_kwargs["event_type"] == "error.new_issue"
    assert call_kwargs["org_id"] == _ORG


async def test_handle_new_issue_payload_fields(
    alerter: ErrorAlerter, mock_dispatcher: AsyncMock
) -> None:
    issue = _fake_issue(
        exception_type="RuntimeError",
        exception_value="connection refused",
        first_release="v2.0.0",
        state="unresolved",
    )
    await alerter.handle_new_issue(issue=issue)
    payload = mock_dispatcher.enqueue_event.call_args.kwargs["payload"]
    assert payload["issue_id"] == str(issue.issue_id)
    assert payload["project_id"] == str(_PROJ)
    assert payload["exception_type"] == "RuntimeError"
    assert payload["exception_value"] == "connection refused"
    assert payload["first_release"] == "v2.0.0"
    assert payload["state"] == "unresolved"
    assert payload["title"] == issue.title
    assert "first_seen" in payload
    assert "last_seen" in payload


async def test_handle_new_issue_returns_enqueue_count(
    alerter: ErrorAlerter, mock_dispatcher: AsyncMock
) -> None:
    mock_dispatcher.enqueue_event.return_value = 3
    issue = _fake_issue()
    count = await alerter.handle_new_issue(issue=issue)
    assert count == 3


# ---------------------------------------------------------------------------
# check_spike
# ---------------------------------------------------------------------------


async def test_check_spike_calls_alert_engine(
    alerter_with_engine: ErrorAlerter, mock_alert_engine: AsyncMock
) -> None:
    from aegis.server.schemas.alerting import AlertRuleResponse

    mock_rule = MagicMock(spec=AlertRuleResponse)
    mock_alert_engine.evaluate_metric.return_value = MagicMock(fired=True)
    result = await alerter_with_engine.check_spike(rule=mock_rule, current_error_rate=0.15)
    mock_alert_engine.evaluate_metric.assert_awaited_once_with(
        rule=mock_rule, current_value=0.15, now=None
    )
    assert result is not None
    assert result.fired is True


async def test_check_spike_returns_none_without_alert_engine(
    alerter: ErrorAlerter,
) -> None:
    from aegis.server.schemas.alerting import AlertRuleResponse

    mock_rule = MagicMock(spec=AlertRuleResponse)
    result = await alerter.check_spike(rule=mock_rule, current_error_rate=0.5)
    assert result is None


# ---------------------------------------------------------------------------
# emit_spike_event
# ---------------------------------------------------------------------------


async def test_emit_spike_event_enqueues_webhook(
    alerter: ErrorAlerter, mock_dispatcher: AsyncMock
) -> None:
    count = await alerter.emit_spike_event(
        org_id=_ORG,
        project_id=_PROJ,
        error_rate=0.12,
        window_seconds=300,
        threshold=0.10,
        severity="warn",
    )
    assert count == 1
    call_kwargs = mock_dispatcher.enqueue_event.call_args.kwargs
    assert call_kwargs["event_type"] == "error.spike"
    assert call_kwargs["org_id"] == _ORG


async def test_emit_spike_event_payload_fields(
    alerter: ErrorAlerter, mock_dispatcher: AsyncMock
) -> None:
    await alerter.emit_spike_event(
        org_id=_ORG,
        project_id=_PROJ,
        error_rate=0.25,
        window_seconds=60,
        threshold=0.20,
        severity="critical",
    )
    payload = mock_dispatcher.enqueue_event.call_args.kwargs["payload"]
    assert payload["project_id"] == str(_PROJ)
    assert payload["error_rate"] == 0.25
    assert payload["window_seconds"] == 60
    assert payload["threshold"] == 0.20
    assert payload["severity"] == "critical"
    assert "detected_at" in payload
