"""Tests for ErrorIngestor — uses mock ErrorEventRepository (no DB)."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from aegis.server.engines.error_ingestor import ErrorIngestor
from aegis.server.schemas.error_monitoring import ErrorEventResponse

_ORG = uuid.UUID("aa010001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("aa010002-0000-0000-0000-000000000000")

_ENVELOPE_HEADER = json.dumps({"sent_at": "2026-06-01T00:00:00Z"})
_ITEM_HEADER = json.dumps({"type": "event"})

_FULL_EVENT = {
    "event_id": "cafebabe",
    "level": "error",
    "environment": "prod",
    "release": "v1.2.3",
    "server_name": "web-01",
    "platform": "python",
    "sdk": {"name": "sentry.python", "version": "2.0.0"},
    "tags": {"region": "us-east"},
    "extra": {"request_id": "xyz"},
    "user": {"id": "u1", "email": "u@example.com"},
    "breadcrumbs": {"values": [{"type": "default", "message": "clicked button"}]},
    "exception": {
        "values": [
            {
                "type": "ValueError",
                "value": "invalid input",
                "stacktrace": {"frames": [{"function": "handle", "filename": "/app/views.py"}]},
            }
        ]
    },
}

_MESSAGE_EVENT = {
    "event_id": "deadbeef",
    "level": "info",
    "message": "something happened",
}


def _make_envelope(*payloads: dict) -> bytes:
    parts = [_ENVELOPE_HEADER]
    for p in payloads:
        parts += [_ITEM_HEADER, json.dumps(p)]
    return ("\n".join(parts) + "\n").encode()


def _fake_response(**kwargs: object) -> ErrorEventResponse:
    defaults = dict(
        event_id=uuid.uuid4(),
        issue_id=None,
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint="envelope-temp-x",
        ts=__import__("datetime").datetime(2026, 6, 1, tzinfo=__import__("datetime").timezone.utc),
        exception_type="ValueError",
        exception_value="invalid input",
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
        received_at=__import__("datetime").datetime(
            2026, 6, 1, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    defaults.update(kwargs)
    return ErrorEventResponse(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.insert.return_value = _fake_response()
    return repo


@pytest.fixture
def ingestor(mock_repo: AsyncMock) -> ErrorIngestor:
    return ErrorIngestor(event_repo=mock_repo)


async def test_ingest_envelope_single_event(ingestor: ErrorIngestor, mock_repo: AsyncMock) -> None:
    raw = _make_envelope(_FULL_EVENT)
    results = await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    assert len(results) == 1
    mock_repo.insert.assert_awaited_once()


async def test_ingest_envelope_multiple_events(
    ingestor: ErrorIngestor, mock_repo: AsyncMock
) -> None:
    raw = _make_envelope(_FULL_EVENT, {**_FULL_EVENT, "event_id": "aabbccdd"})
    results = await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    assert len(results) == 2
    assert mock_repo.insert.await_count == 2


async def test_ingest_envelope_skips_invalid_item(
    ingestor: ErrorIngestor, mock_repo: AsyncMock
) -> None:
    # envelope with one valid event and one event missing both exception and message
    bad_event = {"event_id": "bad000", "level": "error"}  # no exception, no message
    raw = _make_envelope(_FULL_EVENT, bad_event)
    results = await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    # Only the valid event should be persisted
    assert len(results) == 1


async def test_ingest_with_full_context(ingestor: ErrorIngestor, mock_repo: AsyncMock) -> None:
    raw = _make_envelope(_FULL_EVENT)
    mock_repo.insert.return_value = _fake_response(
        tags={"region": "us-east"},
        breadcrumbs=[{"type": "default", "message": "clicked button"}],
        user_context={"id": "u1"},
        release_name="v1.2.3",
        sdk_name="sentry.python",
    )
    results = await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    assert len(results) == 1
    call_args = mock_repo.insert.call_args
    data = call_args.kwargs["data"]
    assert data.exception_type == "ValueError"
    assert data.release_name == "v1.2.3"
    assert data.sdk_name == "sentry.python"
    assert data.tags == {"region": "us-east"}


async def test_ingest_message_only_event(ingestor: ErrorIngestor, mock_repo: AsyncMock) -> None:
    raw = _make_envelope(_MESSAGE_EVENT)
    mock_repo.insert.return_value = _fake_response(exception_type="Message")
    results = await ingestor.ingest_envelope(org_id=_ORG, project_id=_PROJ, envelope_bytes=raw)
    assert len(results) == 1
    data = mock_repo.insert.call_args.kwargs["data"]
    assert data.exception_type == "Message"
    assert data.exception_value == "something happened"
