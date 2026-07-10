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
        conn,
        org_id=_ORG,
        dedup_key="alert:web:cpu",
        title="cpu high",
        severity="critical",
        event_id=_EVENT,
    )
    assert (inc_id, is_new) == (_INC, True)
    # INSERT incident + link event
    assert conn.fetchval.await_count == 1
    insert_link = [c for c in conn.execute.await_args_list if "incident_events" in c.args[0]]
    assert insert_link, "event should be linked"


@pytest.mark.asyncio
async def test_attaches_to_open_incident_and_passes_new_severity() -> None:
    # Escalation is no longer a Python-computed bool passed alongside the new value —
    # it's decided by a rank CASE expression evaluated in SQL against the live row (see
    # _attach()), so a mocked connection can only assert the atomic UPDATE receives the
    # incident id + candidate severity and embeds the rank comparison; the actual
    # escalate-only behavior requires a real Postgres connection to verify.
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"id": _INC}
    inc_id, is_new = await cluster_signal(
        conn,
        org_id=_ORG,
        dedup_key="alert:web:cpu",
        title="cpu high",
        severity="critical",
        event_id=_EVENT,
    )
    assert (inc_id, is_new) == (_INC, False)
    conn.fetchval.assert_not_called()  # no new incident
    upd = next(c for c in conn.execute.await_args_list if "UPDATE incidents" in c.args[0])
    assert upd.args[1] == _INC
    assert upd.args[2] == "critical"
    assert "CASE" in upd.args[0]
    assert "severity" in upd.args[0]


@pytest.mark.asyncio
async def test_attaches_passes_new_severity_even_if_lower() -> None:
    # Whether this ends up demoting severity is now enforced by the live SQL CASE, not by
    # a stale Python-side read — untestable here without a real DB (see comment above).
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"id": _INC}
    await cluster_signal(
        conn,
        org_id=_ORG,
        dedup_key="k",
        title="t",
        severity="warning",
    )
    upd = next(c for c in conn.execute.await_args_list if "UPDATE incidents" in c.args[0])
    assert upd.args[2] == "warning"


@pytest.mark.asyncio
async def test_race_falls_back_to_attach() -> None:
    conn = mock.AsyncMock()
    # 1st SELECT: none; INSERT raises unique violation; 2nd SELECT: now exists
    conn.fetchrow.side_effect = [None, {"id": _INC, "severity": "warning"}]
    conn.fetchval.side_effect = asyncpg.UniqueViolationError("dup")
    inc_id, is_new = await cluster_signal(
        conn,
        org_id=_ORG,
        dedup_key="k",
        title="t",
        severity="critical",
    )
    assert (inc_id, is_new) == (_INC, False)
