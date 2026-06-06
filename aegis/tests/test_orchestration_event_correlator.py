"""Tests for event_trail causal-chain correlator."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from aegis.server.orchestration.event_correlator import correlate_org_events

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_NOW = datetime.now(UTC)


def _ev(
    ev_id: str,
    *,
    service: str = "api",
    trace_id: str | None = None,
    parent_id: str | None = None,
    root_cause_id: str | None = None,
    seconds_ago: int = 0,
) -> dict:
    return {
        "id": str(uuid.UUID(ev_id)),
        "ts": _NOW - timedelta(seconds=seconds_ago),
        "event_type": "alert_fired",
        "severity": "warning",
        "service": service,
        "payload": {},
        "trace_id": trace_id,
        "parent_id": parent_id,
        "root_cause_id": root_cause_id,
        "omodul_kind": None,
        "autoheal_plugin": None,
    }


class TestCorrelateOrgEvents:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_events(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = []
        result = await correlate_org_events(conn=conn, org_id=_ORG)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_events_with_existing_parent_id(self) -> None:
        existing_parent = str(uuid.uuid4())
        ev = _ev(
            "aaaaaaaa-0000-0000-0000-000000000001",
            parent_id=existing_parent,
        )
        conn = mock.AsyncMock()
        conn.fetch.return_value = [ev]
        result = await correlate_org_events(conn=conn, org_id=_ORG)
        # No UPDATE should be issued for already-linked events
        conn.execute.assert_not_called()
        assert result == []

    @pytest.mark.asyncio
    async def test_links_events_with_high_confidence(self) -> None:
        root_id = "aaaaaaaa-0000-0000-0000-000000000001"
        child_id = "bbbbbbbb-0000-0000-0000-000000000002"
        events = [
            _ev(root_id, trace_id="trace-xyz", seconds_ago=10),
            _ev(child_id, trace_id="trace-xyz", seconds_ago=5),
        ]
        conn = mock.AsyncMock()
        conn.fetch.return_value = events
        conn.execute.return_value = "UPDATE 1"

        with mock.patch(
            "aegis.server.orchestration.event_correlator.event_trail_correlate"
        ) as mock_correlate:
            from oskill.event_trail_correlate import CorrelatedEvents

            mock_correlate.return_value = CorrelatedEvents(
                target_event_id=child_id,
                causally_related=[events[0]],
                time_window_correlated=[],
                confidence=0.85,
            )
            result = await correlate_org_events(conn=conn, org_id=_ORG)

        assert child_id in result
        conn.execute.assert_called()

    @pytest.mark.asyncio
    async def test_skips_low_confidence_correlations(self) -> None:
        root_id = "aaaaaaaa-0000-0000-0000-000000000001"
        child_id = "bbbbbbbb-0000-0000-0000-000000000002"
        events = [
            _ev(root_id, seconds_ago=10),
            _ev(child_id, seconds_ago=5),
        ]
        conn = mock.AsyncMock()
        conn.fetch.return_value = events
        conn.execute.return_value = "UPDATE 1"

        with mock.patch(
            "aegis.server.orchestration.event_correlator.event_trail_correlate"
        ) as mock_correlate:
            from oskill.event_trail_correlate import CorrelatedEvents

            mock_correlate.return_value = CorrelatedEvents(
                target_event_id=child_id,
                causally_related=[],
                time_window_correlated=[],
                confidence=0.2,  # below threshold
            )
            result = await correlate_org_events(conn=conn, org_id=_ORG)

        conn.execute.assert_not_called()
        assert result == []

    @pytest.mark.asyncio
    async def test_sets_root_cause_id_from_causally_related(self) -> None:
        root_id = "aaaaaaaa-0000-0000-0000-000000000001"
        child_id = "bbbbbbbb-0000-0000-0000-000000000002"
        events = [
            _ev(root_id, seconds_ago=20),
            _ev(child_id, seconds_ago=5),
        ]
        conn = mock.AsyncMock()
        conn.fetch.return_value = events
        conn.execute.return_value = "UPDATE 1"

        with mock.patch(
            "aegis.server.orchestration.event_correlator.event_trail_correlate"
        ) as mock_correlate:
            from oskill.event_trail_correlate import CorrelatedEvents

            mock_correlate.return_value = CorrelatedEvents(
                target_event_id=child_id,
                causally_related=[events[0]],
                time_window_correlated=[],
                confidence=0.9,
            )
            await correlate_org_events(conn=conn, org_id=_ORG)

        # root_cause_id should be the first causally_related event's id
        call_args = conn.execute.call_args
        assert root_id in str(call_args)
