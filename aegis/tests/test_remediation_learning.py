"""Tests for the remediation learning loop."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import remediation as rem_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.services.remediation_learning import (
    record_outcome,
    success_stats,
    symptom_key,
)

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# ── normalization ────────────────────────────────────────────────────────────────


def test_symptom_key_normalizes_trigger_and_alert_to_same() -> None:
    assert symptom_key("alert:nginx_unhealthy") == "nginx_unhealthy"
    assert symptom_key("Nginx_Unhealthy") == "nginx_unhealthy"
    assert symptom_key("alert:nginx_unhealthy") == symptom_key("nginx_unhealthy")


# ── record ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_outcome_inserts_normalized_key() -> None:
    conn = mock.AsyncMock()
    await record_outcome(
        conn, org_id=_ORG, symptom="alert:CPU_High", remediation="restart-x", success=True
    )
    args = conn.execute.await_args.args
    assert "INSERT INTO remediation_outcomes" in args[0]
    assert args[2] == "cpu_high"  # normalized symptom_key
    assert args[3] == "restart-x" and args[4] is True


@pytest.mark.asyncio
async def test_record_outcome_swallows_errors() -> None:
    conn = mock.AsyncMock()
    conn.execute.side_effect = RuntimeError("db down")
    await record_outcome(conn, org_id=_ORG, symptom="s", remediation="r", success=False)


# ── stats ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_success_stats_computes_rate() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [
        {"remediation": "restart-x", "successes": 9, "total": 10},
        {"remediation": "scale-up", "successes": 1, "total": 4},
    ]
    stats = await success_stats(conn, org_id=_ORG, symptom="cpu_high")
    assert stats[0] == {
        "remediation": "restart-x", "successes": 9, "total": 10, "success_rate": 0.9,
    }
    assert stats[1]["success_rate"] == 0.25


# ── read API ─────────────────────────────────────────────────────────────────────


def _client(conn: mock.AsyncMock, role: str = "viewer") -> TestClient:
    app = FastAPI()
    app.include_router(rem_router.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=uuid.uuid4(), email="a@x.com", orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)]
    )

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def test_stats_endpoint_requires_symptom() -> None:
    conn = mock.AsyncMock()
    r = _client(conn).get(f"/api/v1/orgs/{_ORG}/remediation-stats")
    assert r.status_code == 422  # symptom is required


def test_stats_endpoint_returns_normalized_key_and_list() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"remediation": "r", "successes": 2, "total": 2}]
    r = _client(conn).get(f"/api/v1/orgs/{_ORG}/remediation-stats?symptom=alert:CPU_High")
    assert r.status_code == 200
    body = r.json()
    assert body["symptom_key"] == "cpu_high"
    assert body["remediations"][0]["success_rate"] == 1.0
