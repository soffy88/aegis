"""Tests for Brain pipeline skeleton."""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from aegis.server.orchestration.brain import run_brain_pipeline


@pytest.mark.asyncio
async def test_pipeline_no_escalation_for_info(
    mock_db_conn: mock.AsyncMock,
    test_org_id: uuid.UUID,
    test_project_id: uuid.UUID,
) -> None:
    mock_db_conn.fetchrow.return_value = {"id": str(uuid.uuid4())}

    result = await run_brain_pipeline(
        conn=mock_db_conn,
        org_id=test_org_id,
        project_id=test_project_id,
        user_id=None,
        alert_payload={"alert_name": "test", "severity": "info"},
        trace_id="trc_a",
    )
    assert result["stages_run"] == ["triage"]
    assert result["outcome"] == "no_escalation_needed"


@pytest.mark.asyncio
async def test_pipeline_full_run_for_critical(
    mock_db_conn: mock.AsyncMock,
    test_org_id: uuid.UUID,
    test_project_id: uuid.UUID,
) -> None:
    mock_db_conn.fetchrow.return_value = {"id": str(uuid.uuid4())}

    result = await run_brain_pipeline(
        conn=mock_db_conn,
        org_id=test_org_id,
        project_id=test_project_id,
        user_id=None,
        alert_payload={"alert_name": "x", "severity": "critical"},
        trace_id="trc_b",
    )
    assert result["stages_run"] == ["triage", "rca", "runbook"]
    assert "rca_event_id" in result
    assert "runbook_event_id" in result


@pytest.mark.asyncio
async def test_pipeline_warning_escalates(
    mock_db_conn: mock.AsyncMock,
    test_org_id: uuid.UUID,
    test_project_id: uuid.UUID,
) -> None:
    mock_db_conn.fetchrow.return_value = {"id": str(uuid.uuid4())}

    result = await run_brain_pipeline(
        conn=mock_db_conn,
        org_id=test_org_id,
        project_id=test_project_id,
        user_id=None,
        alert_payload={"alert_name": "x", "severity": "warning"},
        trace_id="trc_c",
    )
    assert "rca" in result["stages_run"]
