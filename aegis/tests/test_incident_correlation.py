"""Tests for incident clustering (services.incident_correlation)."""

from __future__ import annotations

import uuid
from unittest import mock

import asyncpg
import pytest

from aegis.server.services.incident_correlation import cluster_signal

_ORG = uuid.uuid4()
_EVENT = uuid.uuid4()
_INC = uuid.uuid4()


@pytest.mark.asyncio
async def test_opens_new_incident_when_none_open() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = None  # no open incident
    conn.fetchval.return_value = _INC
    inc_id, is_new = await cluster_signal(
        conn, org_id=_ORG, dedup_key="alert:web:cpu", title="cpu high",
        severity="critical", event_id=_EVENT,
    )
    assert (inc_id, is_new) == (_INC, True)
    # INSERT incident + link event
    assert conn.fetchval.await_count == 1
    insert_link = [c for c in conn.execute.await_args_list if "incident_events" in c.args[0]]
    assert insert_link, "event should be linked"


@pytest.mark.asyncio
async def test_attaches_to_open_incident_and_bumps_severity() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"id": _INC, "severity": "warning"}
    inc_id, is_new = await cluster_signal(
        conn, org_id=_ORG, dedup_key="alert:web:cpu", title="cpu high",
        severity="critical", event_id=_EVENT,
    )
    assert (inc_id, is_new) == (_INC, False)
    conn.fetchval.assert_not_called()  # no new incident
    upd = next(c for c in conn.execute.await_args_list if "UPDATE incidents" in c.args[0])
    # bump flag (2nd positional arg) True because critical > warning
    assert upd.args[2] is True


@pytest.mark.asyncio
async def test_does_not_lower_severity() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"id": _INC, "severity": "critical"}
    await cluster_signal(
        conn, org_id=_ORG, dedup_key="k", title="t", severity="warning",
    )
    upd = next(c for c in conn.execute.await_args_list if "UPDATE incidents" in c.args[0])
    assert upd.args[2] is False  # warning < critical → no bump


@pytest.mark.asyncio
async def test_race_falls_back_to_attach() -> None:
    conn = mock.AsyncMock()
    # 1st SELECT: none; INSERT raises unique violation; 2nd SELECT: now exists
    conn.fetchrow.side_effect = [None, {"id": _INC, "severity": "warning"}]
    conn.fetchval.side_effect = asyncpg.UniqueViolationError("dup")
    inc_id, is_new = await cluster_signal(
        conn, org_id=_ORG, dedup_key="k", title="t", severity="critical",
    )
    assert (inc_id, is_new) == (_INC, False)
