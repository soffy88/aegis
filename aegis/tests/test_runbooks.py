"""Tests for Runbook service + endpoints (BATCH 19 §E)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aegis.server.app import create_app
from aegis.server.services.runbook import (
    Runbook,
    RunbookStep,
    StepType,
    _executions,
    _runbooks,
    execute_runbook,
)

client = TestClient(create_app())


@pytest.fixture(autouse=True)
def _reset() -> None:
    _runbooks.clear()
    _executions.clear()


def _register_sample() -> None:
    _runbooks["restart-app"] = Runbook(
        name="restart-app",
        description="Restart a container",
        trigger="alert:cpu_high",
        steps=[
            RunbookStep(name="restart", type=StepType.docker, command="restart myapp"),
        ],
        requires_approval=True,
    )


def test_runbook_yaml_parsing() -> None:
    """Runbook model parses correctly."""
    rb = Runbook(
        name="test",
        steps=[RunbookStep(name="s1", type=StepType.shell, command="echo hi")],
    )
    assert rb.name == "test"
    assert rb.steps[0].type == StepType.shell


@pytest.mark.asyncio
async def test_dry_run_does_not_execute() -> None:
    """Dry run marks steps as would_execute without side effects."""
    _register_sample()
    execution = await execute_runbook("restart-app", dry_run=True)
    assert execution.dry_run is True
    assert execution.steps[0].status == "would_execute"
    assert "DRY RUN" in execution.steps[0].output


@pytest.mark.asyncio
async def test_dry_run_awaits_approval() -> None:
    """Dry run with requires_approval sets awaiting_approval status."""
    _register_sample()
    execution = await execute_runbook("restart-app", dry_run=True)
    assert execution.status == "awaiting_approval"


def test_list_runbooks_endpoint() -> None:
    _register_sample()
    resp = client.get("/api/v1/runbooks")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["name"] == "restart-app"


def test_execute_dry_run_endpoint() -> None:
    _register_sample()
    resp = client.post("/api/v1/runbooks/restart-app/execute", json={"dry_run": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "awaiting_approval"
    assert data["steps"][0]["status"] == "would_execute"


def test_execute_not_found() -> None:
    resp = client.post("/api/v1/runbooks/ghost/execute", json={"dry_run": True})
    assert resp.status_code == 404
