"""Tests for the scrape-targets router (unit, mocked DB)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import scrape_targets as router_mod
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_TID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _user(role: str = "admin") -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        email="t@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)],
    )


def _target_row(**over) -> dict:
    base = {
        "id": _TID, "org_id": _ORG, "name": "node", "url": "http://localhost:9100/metrics",
        "interval_seconds": 30, "labels": {}, "enabled": True,
        "last_scrape_at": None, "last_status": None, "last_error": None, "created_at": _NOW,
    }
    base.update(over)
    return base


@pytest.fixture
def conn() -> mock.AsyncMock:
    return mock.AsyncMock()


def _client(conn: mock.AsyncMock, role: str = "admin") -> TestClient:
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: _user(role)

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def test_create_target_201(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = _target_row()
    r = _client(conn).post(
        f"/api/v1/orgs/{_ORG}/scrape-targets",
        json={"name": "node", "url": "http://localhost:9100/metrics"},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "node"


def test_create_rejects_bad_url(conn: mock.AsyncMock) -> None:
    r = _client(conn).post(
        f"/api/v1/orgs/{_ORG}/scrape-targets",
        json={"name": "x", "url": "ftp://nope"},
    )
    assert r.status_code == 422


def test_create_allows_private_localhost(conn: mock.AsyncMock) -> None:
    # Scrape targets are intentionally internal — localhost must NOT be blocked.
    conn.fetchrow.return_value = _target_row()
    r = _client(conn).post(
        f"/api/v1/orgs/{_ORG}/scrape-targets",
        json={"name": "n", "url": "http://127.0.0.1:9090/metrics"},
    )
    assert r.status_code == 201


def test_viewer_cannot_create(conn: mock.AsyncMock) -> None:
    r = _client(conn, role="viewer").post(
        f"/api/v1/orgs/{_ORG}/scrape-targets",
        json={"name": "n", "url": "http://localhost/metrics"},
    )
    assert r.status_code == 403


def test_list_targets(conn: mock.AsyncMock) -> None:
    conn.fetch.return_value = [_target_row()]
    r = _client(conn, role="viewer").get(f"/api/v1/orgs/{_ORG}/scrape-targets")
    assert r.status_code == 200
    assert r.json()[0]["url"].endswith("/metrics")


def test_delete_404_when_absent(conn: mock.AsyncMock) -> None:
    conn.execute.return_value = "DELETE 0"
    r = _client(conn).delete(f"/api/v1/orgs/{_ORG}/scrape-targets/{_TID}")
    assert r.status_code == 404
