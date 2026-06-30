"""Tests for incident ack, MTTA/MTTR metrics, and the public status page."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import incidents as inc_router
from aegis.server.api.routers import status_page
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_INC = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _user(role: str = "operator") -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(), email="t@x.com",
        orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)],
    )


@pytest.fixture
def conn() -> mock.AsyncMock:
    return mock.AsyncMock()


def _client(routers: list, conn: mock.AsyncMock, *, role: str = "operator", auth: bool = True) -> TestClient:
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    if auth:
        app.dependency_overrides[get_current_user] = lambda: _user(role)

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


# ── ack ──────────────────────────────────────────────────────────────────────────


def test_ack_sets_timestamp(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": _INC, "acknowledged_at": _NOW, "acknowledged_by": uuid.uuid4()}
    r = _client([inc_router.router], conn).post(f"/api/v1/orgs/{_ORG}/incidents/{_INC}/ack")
    assert r.status_code == 200
    assert r.json()["acknowledged_at"] is not None


def test_ack_404(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = None
    r = _client([inc_router.router], conn).post(f"/api/v1/orgs/{_ORG}/incidents/{_INC}/ack")
    assert r.status_code == 404


def test_viewer_cannot_ack(conn: mock.AsyncMock) -> None:
    r = _client([inc_router.router], conn, role="viewer").post(
        f"/api/v1/orgs/{_ORG}/incidents/{_INC}/ack"
    )
    assert r.status_code == 403


# ── metrics ──────────────────────────────────────────────────────────────────────


def test_incident_metrics(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {
        "total": 10, "open": 2, "resolved": 8, "mtta_seconds": 120.5, "mttr_seconds": 3600.0,
    }
    r = _client([status_page.router], conn, role="viewer").get(
        f"/api/v1/orgs/{_ORG}/incident-metrics"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mtta_seconds"] == 120.5 and body["mttr_seconds"] == 3600.0
    assert body["open"] == 2


def test_metrics_handles_no_data(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {
        "total": 0, "open": 0, "resolved": 0, "mtta_seconds": None, "mttr_seconds": None,
    }
    r = _client([status_page.router], conn, role="viewer").get(
        f"/api/v1/orgs/{_ORG}/incident-metrics"
    )
    assert r.json()["mtta_seconds"] is None


# ── public status page (no auth) ─────────────────────────────────────────────────


def test_public_status_operational(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": _ORG}
    conn.fetch.return_value = []
    r = _client([status_page.router], conn, auth=False).get("/api/v1/status/o")
    assert r.status_code == 200
    assert r.json()["status"] == "operational"


def test_public_status_major_outage_when_critical(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": _ORG}
    conn.fetch.return_value = [{"title": "db down", "severity": "critical", "started_at": _NOW}]
    r = _client([status_page.router], conn, auth=False).get("/api/v1/status/o")
    assert r.json()["status"] == "major_outage"
    assert r.json()["open_incidents"][0]["title"] == "db down"


def test_public_status_unknown_slug_404(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = None
    r = _client([status_page.router], conn, auth=False).get("/api/v1/status/nope")
    assert r.status_code == 404
