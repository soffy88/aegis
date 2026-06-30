"""Tests for the new incident endpoints (create / resolve / events)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import incidents as inc_router
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


def _client(conn: mock.AsyncMock, role: str = "operator") -> TestClient:
    app = FastAPI()
    app.include_router(inc_router.router)
    app.dependency_overrides[get_current_user] = lambda: _user(role)

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def test_create_incident_201(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {
        "id": _INC, "org_id": _ORG, "title": "manual", "started_at": _NOW,
        "resolved_at": None, "severity": "warning", "status": "open",
        "postmortem_md": None, "created_at": _NOW, "dedup_key": None,
        "event_count": 0, "last_event_at": None,
    }
    r = _client(conn).post(f"/api/v1/orgs/{_ORG}/incidents", json={"title": "manual"})
    assert r.status_code == 201
    assert r.json()["status"] == "open"


def test_viewer_cannot_create(conn: mock.AsyncMock) -> None:
    r = _client(conn, role="viewer").post(
        f"/api/v1/orgs/{_ORG}/incidents", json={"title": "x"}
    )
    assert r.status_code == 403


def test_resolve_open_incident(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": _INC, "status": "resolved", "resolved_at": _NOW}
    r = _client(conn).post(f"/api/v1/orgs/{_ORG}/incidents/{_INC}/resolve")
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"


def test_resolve_404_when_not_open(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = None
    r = _client(conn).post(f"/api/v1/orgs/{_ORG}/incidents/{_INC}/resolve")
    assert r.status_code == 404


def test_list_incident_events_404_when_incident_absent(conn: mock.AsyncMock) -> None:
    conn.fetchval.return_value = None  # ownership check fails
    r = _client(conn, role="viewer").get(f"/api/v1/orgs/{_ORG}/incidents/{_INC}/events")
    assert r.status_code == 404


def test_list_incident_events_returns_linked(conn: mock.AsyncMock) -> None:
    conn.fetchval.return_value = 1  # owns
    conn.fetch.return_value = [
        {"id": uuid.uuid4(), "ts": _NOW, "event_type": "alert_fired", "severity": "critical",
         "service": "web", "payload": {}, "trace_id": "t"}
    ]
    r = _client(conn, role="viewer").get(f"/api/v1/orgs/{_ORG}/incidents/{_INC}/events")
    assert r.status_code == 200
    assert r.json()[0]["event_type"] == "alert_fired"
