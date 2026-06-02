"""Tests for ErrorAggregator — RUN_SMOKE=1 (uses testcontainers for DB tests)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from aegis.server.engines.error_aggregator import ErrorAggregator
from aegis.server.repositories.error_event_repository import ErrorEventRepository
from aegis.server.repositories.error_issue_repository import ErrorIssueRepository
from aegis.server.schemas.error_monitoring import ErrorEventResponse

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"

_ORG = uuid.UUID("fa040001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("fa040002-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_event(**kwargs: Any) -> ErrorEventResponse:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    defaults: dict[str, Any] = dict(
        event_id=uuid.uuid4(),
        issue_id=None,
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint="envelope-temp-placeholder",
        ts=now,
        exception_type="TypeError",
        exception_value="bad arg",
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
        received_at=now,
    )
    defaults.update(kwargs)
    return ErrorEventResponse(**defaults)


def _make_issue_response(
    issue_id: uuid.UUID | None = None,
    fingerprint: str = "fp-real",
) -> Any:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    from aegis.server.schemas.error_monitoring import ErrorIssueResponse

    return ErrorIssueResponse(
        issue_id=issue_id or uuid.uuid4(),
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint=fingerprint,
        exception_type="TypeError",
        exception_value="bad arg",
        title="TypeError: bad arg",
        event_count=1,
        user_count=0,
        first_seen=now,
        last_seen=now,
        state="unresolved",
        first_release=None,
        last_release=None,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# Unit tests (mock repos)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_event_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.update_fingerprint_and_issue.return_value = True
    return repo


@pytest.fixture
def mock_issue_repo() -> AsyncMock:
    repo = AsyncMock()
    repo.upsert_by_fingerprint.return_value = (_make_issue_response(), True)
    return repo


@pytest.fixture
def aggregator(mock_event_repo: AsyncMock, mock_issue_repo: AsyncMock) -> ErrorAggregator:
    return ErrorAggregator(event_repo=mock_event_repo, issue_repo=mock_issue_repo)


async def test_aggregate_event_new_issue(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    issue = _make_issue_response()
    mock_issue_repo.upsert_by_fingerprint.return_value = (issue, True)
    event = _fake_event()
    result = await aggregator.aggregate_event(event=event)
    assert result.issue_id == issue.issue_id
    assert result.fingerprint != "envelope-temp-placeholder"
    assert len(result.fingerprint) == 64
    mock_event_repo.update_fingerprint_and_issue.assert_awaited_once()


async def test_aggregate_event_existing_issue(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    existing_issue_id = uuid.uuid4()
    issue = _make_issue_response(issue_id=existing_issue_id)
    mock_issue_repo.upsert_by_fingerprint.return_value = (issue, False)
    event = _fake_event()
    result = await aggregator.aggregate_event(event=event)
    assert result.issue_id == existing_issue_id
    mock_issue_repo.upsert_by_fingerprint.assert_awaited_once()


async def test_aggregate_event_calls_compute_event_fingerprint(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    event = _fake_event(
        exception_type="ValueError",
        exception_value="out of range",
        stacktrace={"frames": [{"function": "run", "filename": "/app/main.py"}]},
    )
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "a" * 64
        await aggregator.aggregate_event(event=event)
    mock_fp.assert_called_once_with(
        exception_type="ValueError",
        exception_value="out of range",
        top_frame_function="run",
        top_frame_filename="/app/main.py",
        custom_fingerprint=None,
    )


async def test_aggregate_event_with_custom_fingerprint(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    event = _fake_event()
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "b" * 64
        await aggregator.aggregate_event(
            event=event,
            custom_fingerprint=["payment-flow", "retry-exhausted"],
        )
    mock_fp.assert_called_once_with(
        exception_type="TypeError",
        exception_value="bad arg",
        top_frame_function=None,
        top_frame_filename=None,
        custom_fingerprint=["payment-flow", "retry-exhausted"],
    )


async def test_aggregate_event_message_only_no_stacktrace(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    event = _fake_event(exception_type="Message", exception_value="info log", stacktrace=None)
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "c" * 64
        await aggregator.aggregate_event(event=event)
    mock_fp.assert_called_once_with(
        exception_type="Message",
        exception_value="info log",
        top_frame_function=None,
        top_frame_filename=None,
        custom_fingerprint=None,
    )


async def test_aggregate_event_with_stacktrace_extracts_top_frame(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    stacktrace = {
        "frames": [
            {"function": "outer", "filename": "/app/outer.py"},
            {"function": "inner", "filename": "/app/inner.py"},
        ]
    }
    event = _fake_event(stacktrace=stacktrace)
    with patch("aegis.server.engines.error_aggregator.compute_event_fingerprint") as mock_fp:
        mock_fp.return_value = "d" * 64
        await aggregator.aggregate_event(event=event)
    _, kwargs = mock_fp.call_args
    assert kwargs["top_frame_function"] == "inner"
    assert kwargs["top_frame_filename"] == "/app/inner.py"


async def test_aggregate_event_different_exception_creates_new_issue(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    issue_a = _make_issue_response(fingerprint="fp-a")
    issue_b = _make_issue_response(fingerprint="fp-b")
    mock_issue_repo.upsert_by_fingerprint.side_effect = [(issue_a, True), (issue_b, True)]
    event_a = _fake_event(exception_type="TypeError")
    event_b = _fake_event(exception_type="ValueError")
    result_a = await aggregator.aggregate_event(event=event_a)
    result_b = await aggregator.aggregate_event(event=event_b)
    assert result_a.issue_id != result_b.issue_id


async def test_aggregate_event_same_exception_same_issue(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    shared_issue = _make_issue_response()
    mock_issue_repo.upsert_by_fingerprint.side_effect = [
        (shared_issue, True),
        (shared_issue, False),
    ]
    event1 = _fake_event()
    event2 = _fake_event(event_id=uuid.uuid4())
    result1 = await aggregator.aggregate_event(event=event1)
    result2 = await aggregator.aggregate_event(event=event2)
    assert result1.issue_id == result2.issue_id


async def test_aggregate_event_release_tracking(
    aggregator: ErrorAggregator,
    mock_event_repo: AsyncMock,
    mock_issue_repo: AsyncMock,
) -> None:
    event = _fake_event(release_name="v2.0.0")
    await aggregator.aggregate_event(event=event)
    call_kwargs = mock_issue_repo.upsert_by_fingerprint.call_args.kwargs
    assert call_kwargs["release_name"] == "v2.0.0"


# ---------------------------------------------------------------------------
# Smoke tests (real DB via testcontainers)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container_agg() -> Generator[Any, None, None]:
    if not RUN_SMOKE:
        pytest.skip("set RUN_SMOKE=1 to run")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("timescale/timescaledb:2.26.3-pg18") as pg:
        yield pg


@pytest.fixture
async def conn_agg(pg_container_agg: Any) -> AsyncGenerator[asyncpg.Connection, None]:
    dsn = pg_container_agg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    c = await asyncpg.connect(dsn)
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(c)
    await c.execute(
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'agg-test', 'AGG', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG,
    )
    await c.execute(
        "INSERT INTO projects (id, org_id, name, slug, display_name)"
        " VALUES ($1,$2,'agg-proj','agg-proj','AGG Proj') ON CONFLICT DO NOTHING",
        _PROJ,
        _ORG,
    )
    try:
        yield c
    finally:
        await c.close()


@pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")
async def test_aggregate_event_updates_event_record(
    conn_agg: asyncpg.Connection,
) -> None:
    from aegis.server.schemas.error_monitoring import ErrorEventCreate

    event_repo = ErrorEventRepository(conn_agg)
    issue_repo = ErrorIssueRepository(conn_agg)
    agg = ErrorAggregator(event_repo=event_repo, issue_repo=issue_repo)

    # Insert event with placeholder fingerprint
    ev = await event_repo.insert(
        data=ErrorEventCreate(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint="envelope-temp-smoke",
            exception_type="OSError",
            exception_value="file not found",
            level="error",
        )
    )
    assert ev.fingerprint == "envelope-temp-smoke"
    assert ev.issue_id is None

    # Aggregate
    result = await agg.aggregate_event(event=ev)

    assert result.fingerprint != "envelope-temp-smoke"
    assert len(result.fingerprint) == 64
    assert result.issue_id is not None

    # Verify DB row was updated
    rows = await conn_agg.fetch(
        "SELECT fingerprint, issue_id FROM error_events WHERE event_id = $1",
        ev.event_id,
    )
    assert rows[0]["fingerprint"] == result.fingerprint
    assert rows[0]["issue_id"] == result.issue_id
