"""Tests for app upgrade/rollback lifecycle (version bookkeeping)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import apps as apps_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_APP = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def _user(role: str = "member") -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(), email="t@x.com",
        orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)],
    )


@pytest.fixture(autouse=True)
def _no_bg() -> AsyncIterator[None]:
    # Don't run the real omodul/pool background hook.
    with mock.patch(
        "aegis.server.api.routers.apps._run_app_lifecycle", new_callable=mock.AsyncMock
    ):
        yield


@pytest.fixture
def conn() -> mock.AsyncMock:
    return mock.AsyncMock()


def _client(conn: mock.AsyncMock, role: str = "member") -> TestClient:
    app = FastAPI()
    app.include_router(apps_router.router)
    app.dependency_overrides[get_current_user] = lambda: _user(role)

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


# ── upgrade ──────────────────────────────────────────────────────────────────────


def test_upgrade_records_previous_version(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {"app_name": "redis", "app_version": "7.2"}
    r = _client(conn).post(
        f"/api/v1/orgs/{_ORG}/apps/{_APP}/upgrade", json={"target_version": "7.4"}
    )
    assert r.status_code == 202
    assert r.json() == {
        "install_id": str(_APP), "status": "upgrading",
        "from_version": "7.2", "to_version": "7.4",
    }
    upd = next(c for c in conn.execute.await_args_list if "previous_version = app_version" in c.args[0])
    assert "app_version = $3" in upd.args[0]


def test_upgrade_404_when_missing(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = None
    r = _client(conn).post(
        f"/api/v1/orgs/{_ORG}/apps/{_APP}/upgrade", json={"target_version": "9"}
    )
    assert r.status_code == 404


def test_viewer_cannot_upgrade(conn: mock.AsyncMock) -> None:
    r = _client(conn, role="viewer").post(
        f"/api/v1/orgs/{_ORG}/apps/{_APP}/upgrade", json={"target_version": "9"}
    )
    assert r.status_code == 403  # INSTALL_APP is member+


# ── rollback ─────────────────────────────────────────────────────────────────────


def test_rollback_swaps_versions(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {
        "app_name": "redis", "app_version": "7.4", "previous_version": "7.2",
    }
    r = _client(conn).post(f"/api/v1/orgs/{_ORG}/apps/{_APP}/rollback")
    assert r.status_code == 202
    assert r.json()["rolled_back_to"] == "7.2"
    upd = next(c for c in conn.execute.await_args_list if "rolling_back" in c.args[0])
    assert "app_version = previous_version" in upd.args[0]


def test_rollback_400_without_previous(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = {
        "app_name": "redis", "app_version": "7.4", "previous_version": None,
    }
    r = _client(conn).post(f"/api/v1/orgs/{_ORG}/apps/{_APP}/rollback")
    assert r.status_code == 400


def test_rollback_404_when_missing(conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = None
    r = _client(conn).post(f"/api/v1/orgs/{_ORG}/apps/{_APP}/rollback")
    assert r.status_code == 404
