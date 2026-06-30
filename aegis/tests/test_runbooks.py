"""Tests for Runbook service + endpoints (C1-4 org-scoped paths)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import runbooks as runbooks_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.services.runbook import (
    Runbook,
    RunbookStep,
    StepType,
    _executions,
    _runbooks,
    execute_runbook,
)

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="owner")],
    )


def _project_row() -> dict:
    return {
        "id": _PROJ,
        "org_id": _ORG,
        "slug": "test-proj",
        "name": "Test Project",
        "display_name": "Test Project",
        "environment": "prod",
        "docker_labels": None,
        "config": None,
        "archived_at": None,
        "created_at": datetime(2026, 1, 1),
    }


@pytest.fixture
def rb_conn() -> mock.AsyncMock:
    m = mock.AsyncMock()
    m.fetchrow.return_value = _project_row()
    return m


@pytest.fixture
def client(rb_conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(runbooks_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield rb_conn

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


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


def test_list_runbooks_endpoint(client: TestClient) -> None:
    _register_sample()
    resp = client.get(f"/api/v1/orgs/{_ORG}/runbooks")
    assert resp.status_code == 200
    # Robust to whether the aegis-plugins pack is installed (it merges in entry-point
    # plugins): assert the YAML runbook is present rather than an exact count.
    names = [r["name"] for r in resp.json()]
    assert "restart-app" in names
    yaml_entry = next(r for r in resp.json() if r["name"] == "restart-app")
    assert yaml_entry["source"] == "yaml"


def test_execute_dry_run_endpoint(client: TestClient) -> None:
    _register_sample()
    resp = client.post(
        f"/api/v1/orgs/{_ORG}/runbooks/restart-app/execute?project_id={_PROJ}",
        json={"dry_run": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "awaiting_approval"
    assert data["steps"][0]["status"] == "would_execute"


def test_execute_not_found(client: TestClient) -> None:
    resp = client.post(
        f"/api/v1/orgs/{_ORG}/runbooks/ghost/execute?project_id={_PROJ}",
        json={"dry_run": True},
    )
    assert resp.status_code == 404
