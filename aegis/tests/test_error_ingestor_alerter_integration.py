"""Tests for ErrorIngestor + ErrorAlerter integration."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aegis.server.engines.error_aggregator import ErrorAggregator
from aegis.server.engines.error_alerter import ErrorAlerter
from aegis.server.engines.error_ingestor import ErrorIngestor
from aegis.server.schemas.error_monitoring import ErrorEventResponse, ErrorIssueResponse

_ORG = uuid.UUID("fd060001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("fd060002-0000-0000-0000-000000000000")

_EH = json.dumps({"sent_at": "2026-06-01T00:00:00Z"})
_IH = json.dumps({"type": "event"})

_EVENT = {
    "event_id": "cafe1111",
    "level": "error",
    "exception": {"values": [{"type": "KeyError", "value": "missing key"}]},
}


def _now() -> datetime:
    return datetime(2026, 6, 1, tzinfo=UTC)


def _make_envelope(*payloads: dict) -> bytes:  # type: ignore[type-arg]
    parts = [_EH]
    for p in payloads:
        parts += [_IH, json.dumps(p)]
    return ("\n".join(parts) + "\n").encode()


def _fake_event(**kwargs: Any) -> ErrorEventResponse:
    defaults: dict[str, Any] = dict(
        event_id=uuid.uuid4(),
        issue_id=None,
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint="envelope-temp-x",
        ts=_now(),
        exception_type="KeyError",
        exception_value="missing key",
        level="error",
        environment="prod",
        server_name=None,
        release_name=None,
        stacktrace=None,
        breadcrumbs=None,
        user_context=None,
        tags=None,
        extra=None,
        sdk_name=None,
        sdk_version=None,
        platform=None,
        received_at=_now(),
    )
    defaults.update(kwargs)
    return ErrorEventResponse(**defaults)


def _fake_issue(is_new: bool = True) -> ErrorIssueResponse:
    return ErrorIssueResponse(
        issue_id=uuid.uuid4(),
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint="a" * 64,
        exception_type="KeyError",
        exception_value="missing key",
        title="KeyError: missing key",
        event_count=1,
        user_count=0,
        first_seen=_now(),
        last_seen=_now(),
        state="unresolved",
        first_release=None,
        last_release=None,
        created_at=_now(),
        updated_at=_now(),
    )


@pytest.fixture
def mock_event_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.insert.return_value = _fake_event()
    repo.update_fingerprint_and_issue.return_value = True
    return repo


@pytest.fixture
def mock_issue_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.upsert_by_fingerprint.return_value = (_fake_issue(), True)
    return repo


@pytest.fixture
def mock_dispatcher() -> AsyncMock:
    d = AsyncMock()
    d.enqueue_event.return_value = 1
    return d


@pytest.fixture
def aggregator(mock_event_repo: AsyncMock, mock_issue_repo: AsyncMock) -> ErrorAggregator:
    return ErrorAggregator(event_repo=mock_event_repo, issue_repo=mock_issue_repo)


@pytest.fixture
def alerter(mock_dispatcher: AsyncMock) -> ErrorAlerter:
    return ErrorAlerter(webhook_dispatcher=mock_dispatcher)


async def test_ingestor_calls_alerter_when_new_issue(
    mock_event_repo: AsyncMock,
    aggregator: ErrorAggregator,
    alerter: ErrorAlerter,
    mock_dispatcher: AsyncMock,
) -> None:
    ingestor = ErrorIngestor(event_repo=mock_event_repo, aggregator=aggregator, alerter=alerter)
    raw = _make_envelope(_EVENT)
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "a" * 64
        results = await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    assert len(results) == 1
    mock_dispatcher.enqueue_event.assert_awaited_once()
    call_kwargs = mock_dispatcher.enqueue_event.call_args.kwargs
    assert call_kwargs["event_type"] == "error.new_issue"


async def test_ingestor_skips_alerter_when_existing_issue(
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
    alerter: ErrorAlerter,
    mock_dispatcher: AsyncMock,
) -> None:
    # Existing issue → is_new=False
    mock_issue_repo.upsert_by_fingerprint.return_value = (_fake_issue(), False)
    agg = ErrorAggregator(event_repo=mock_event_repo, issue_repo=mock_issue_repo)
    ingestor = ErrorIngestor(event_repo=mock_event_repo, aggregator=agg, alerter=alerter)
    raw = _make_envelope(_EVENT)
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "b" * 64
        await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    mock_dispatcher.enqueue_event.assert_not_called()


async def test_ingestor_without_alerter_works_backward_compat(
    mock_event_repo: AsyncMock,
    aggregator: ErrorAggregator,
) -> None:
    ingestor = ErrorIngestor(event_repo=mock_event_repo, aggregator=aggregator)
    raw = _make_envelope(_EVENT)
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "c" * 64
        results = await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    assert len(results) == 1


async def test_ingestor_calls_alerter_only_for_new_issues_in_batch(
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
    alerter: ErrorAlerter,
    mock_dispatcher: AsyncMock,
) -> None:
    # Event 1: new issue, Event 2: existing issue
    ev2 = _fake_event(event_id=uuid.uuid4())
    mock_event_repo.insert.side_effect = [_fake_event(), ev2]
    mock_issue_repo.upsert_by_fingerprint.side_effect = [
        (_fake_issue(), True),  # new
        (_fake_issue(), False),  # existing
    ]
    agg = ErrorAggregator(event_repo=mock_event_repo, issue_repo=mock_issue_repo)
    ingestor = ErrorIngestor(event_repo=mock_event_repo, aggregator=agg, alerter=alerter)
    raw = _make_envelope(_EVENT, {**_EVENT, "event_id": "cafe2222"})
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "d" * 64
        results = await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    assert len(results) == 2
    # Only 1 alerter call (for the new issue), not 2
    assert mock_dispatcher.enqueue_event.await_count == 1
