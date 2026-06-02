"""Tests for ErrorIngestor + ErrorAggregator integration."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from aegis.server.engines.error_aggregator import ErrorAggregator
from aegis.server.engines.error_ingestor import ErrorIngestor
from aegis.server.schemas.error_monitoring import ErrorEventResponse, ErrorIssueResponse

_ORG = uuid.UUID("fb050001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("fb050002-0000-0000-0000-000000000000")

_EH = json.dumps({"sent_at": "2026-06-01T00:00:00Z"})
_IH = json.dumps({"type": "event"})

_EVENT = {
    "event_id": "cafe0001",
    "level": "error",
    "exception": {
        "values": [
            {
                "type": "RuntimeError",
                "value": "connection refused",
                "stacktrace": {"frames": [{"function": "connect", "filename": "/app/db.py"}]},
            }
        ]
    },
}


def _make_envelope(*payloads: dict) -> bytes:
    parts = [_EH]
    for p in payloads:
        parts += [_IH, json.dumps(p)]
    return ("\n".join(parts) + "\n").encode()


def _now() -> datetime:
    return datetime(2026, 6, 1, tzinfo=UTC)


def _fake_event(**kwargs: Any) -> ErrorEventResponse:
    defaults: dict[str, Any] = dict(
        event_id=uuid.uuid4(),
        issue_id=None,
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint="envelope-temp-x",
        ts=_now(),
        exception_type="RuntimeError",
        exception_value="connection refused",
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


def _fake_issue() -> ErrorIssueResponse:
    return ErrorIssueResponse(
        issue_id=uuid.uuid4(),
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint="a" * 64,
        exception_type="RuntimeError",
        exception_value="connection refused",
        title="RuntimeError: connection refused",
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
def aggregator(mock_event_repo: AsyncMock, mock_issue_repo: AsyncMock) -> ErrorAggregator:
    return ErrorAggregator(event_repo=mock_event_repo, issue_repo=mock_issue_repo)


@pytest.fixture
def ingestor_with_agg(mock_event_repo: AsyncMock, aggregator: ErrorAggregator) -> ErrorIngestor:
    return ErrorIngestor(event_repo=mock_event_repo, aggregator=aggregator)


@pytest.fixture
def ingestor_no_agg(mock_event_repo: AsyncMock) -> ErrorIngestor:
    return ErrorIngestor(event_repo=mock_event_repo)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_ingestor_with_aggregator_returns_full_event(
    ingestor_with_agg: ErrorIngestor,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    raw = _make_envelope(_EVENT)
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "a" * 64
        results = await ingestor_with_agg.ingest_envelope(
            org_id=_ORG, project_id=_PROJ, envelope_bytes=raw
        )
    assert len(results) == 1
    assert results[0].fingerprint == "a" * 64
    assert results[0].issue_id is not None
    mock_event_repo.update_fingerprint_and_issue.assert_awaited_once()


async def test_ingestor_without_aggregator_keeps_placeholder(
    ingestor_no_agg: ErrorIngestor,
    mock_event_repo: AsyncMock,
) -> None:
    raw = _make_envelope(_EVENT)
    results = await ingestor_no_agg.ingest_envelope(
        org_id=_ORG, project_id=_PROJ, envelope_bytes=raw
    )
    assert len(results) == 1
    assert results[0].fingerprint == "envelope-temp-x"
    mock_event_repo.update_fingerprint_and_issue.assert_not_called()


async def test_ingestor_with_aggregator_handles_envelope_custom_fingerprint(
    ingestor_with_agg: ErrorIngestor,
) -> None:
    custom_fp_event = {**_EVENT, "fingerprint": ["custom-group", "v2"]}
    raw = _make_envelope(custom_fp_event)
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "b" * 64
        await ingestor_with_agg.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    _, kwargs = mock_fp.call_args
    assert kwargs["custom_fingerprint"] == ["custom-group", "v2"]


async def test_ingestor_multiple_events_each_aggregated(
    ingestor_with_agg: ErrorIngestor,
    mock_event_repo: AsyncMock,
) -> None:
    ev2 = _fake_event(event_id=uuid.uuid4(), fingerprint="envelope-temp-y")
    mock_event_repo.insert.side_effect = [_fake_event(), ev2]
    raw = _make_envelope(_EVENT, {**_EVENT, "event_id": "cafe0002"})
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "c" * 64
        results = await ingestor_with_agg.ingest_envelope(
            org_id=_ORG, project_id=_PROJ, envelope_bytes=raw
        )
    assert len(results) == 2
    assert mock_event_repo.update_fingerprint_and_issue.await_count == 2


async def test_ingestor_with_aggregator_invalid_event_still_skipped(
    ingestor_with_agg: ErrorIngestor,
    mock_event_repo: AsyncMock,
) -> None:
    bad_event = {"event_id": "bad000", "level": "error"}  # no exception, no message
    raw = _make_envelope(_EVENT, bad_event)
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "d" * 64
        results = await ingestor_with_agg.ingest_envelope(
            org_id=_ORG, project_id=_PROJ, envelope_bytes=raw
        )
    assert len(results) == 1
