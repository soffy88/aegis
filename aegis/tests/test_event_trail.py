"""Tests for event_trail writer/reader (DB layer)."""
from __future__ import annotations

import uuid
from unittest import mock

import pytest

from aegis.server.persistence.event_trail import (
    append_event,
    causal_chain,
    recent_events,
)


@pytest.mark.asyncio
async def test_append_event_basic(
    mock_db_conn: mock.AsyncMock,
    test_org_id: uuid.UUID,
    test_project_id: uuid.UUID,
) -> None:
    new_id = uuid.uuid4()
    mock_db_conn.fetchrow.return_value = {"id": str(new_id)}

    result = await append_event(
        conn=mock_db_conn,
        org_id=test_org_id, project_id=test_project_id,
        event_type="alert_fired",
        payload={"a": 1},
    )
    assert result == new_id
    mock_db_conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_append_event_with_all_fields(
    mock_db_conn: mock.AsyncMock,
    test_org_id: uuid.UUID,
    test_project_id: uuid.UUID,
) -> None:
    new_id = uuid.uuid4()
    mock_db_conn.fetchrow.return_value = {"id": str(new_id)}
    parent = uuid.uuid4()

    await append_event(
        conn=mock_db_conn,
        org_id=test_org_id, project_id=test_project_id,
        event_type="omodul_run",
        severity="warning",
        payload={"k": "v"},
        service="svc",
        resource="r",
        trace_id="trc_x",
        parent_id=parent,
        omodul_kind="diagnose_x",
        omodul_fingerprint="fp_abc",
        autoheal_plugin="restart",
        autoheal_result={"ok": True},
        initiated_by="agent",
    )
    call = mock_db_conn.fetchrow.call_args
    # Verify the parent_id and autoheal_plugin were passed in args
    assert parent in call[0]
    assert "restart" in call[0]


@pytest.mark.asyncio
async def test_recent_events_no_service(
    mock_db_conn: mock.AsyncMock,
    test_org_id: uuid.UUID,
    test_project_id: uuid.UUID,
) -> None:
    mock_db_conn.fetch.return_value = [
        {"id": uuid.uuid4(), "event_type": "alert_fired", "ts": "x",
         "severity": "warning", "payload": {}, "omodul_kind": None,
         "autoheal_plugin": None, "trace_id": None},
    ]
    result = await recent_events(
        conn=mock_db_conn,
        org_id=test_org_id, project_id=test_project_id,
    )
    assert len(result) == 1


@pytest.mark.asyncio
async def test_recent_events_filtered_by_service(
    mock_db_conn: mock.AsyncMock,
    test_org_id: uuid.UUID,
    test_project_id: uuid.UUID,
) -> None:
    mock_db_conn.fetch.return_value = []
    await recent_events(
        conn=mock_db_conn,
        org_id=test_org_id, project_id=test_project_id,
        service="helixa",
    )
    call = mock_db_conn.fetch.call_args
    assert "helixa" in call[0]


@pytest.mark.asyncio
async def test_causal_chain(mock_db_conn: mock.AsyncMock) -> None:
    eid = uuid.uuid4()
    mock_db_conn.fetch.return_value = [
        {"id": eid, "parent_id": None, "event_type": "x",
         "payload": {}, "ts": "t", "depth": 0},
    ]
    result = await causal_chain(conn=mock_db_conn, event_id=eid)
    assert len(result) == 1
