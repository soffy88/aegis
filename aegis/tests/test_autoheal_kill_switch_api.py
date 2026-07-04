"""API tests for the §5.3 global autoheal kill switch (GET/PUT /autoheal/kill-switch)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import autoheal as autoheal_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _client(role: str, conn: mock.AsyncMock) -> TestClient:
    fa = FastAPI()
    fa.include_router(autoheal_router.router)

    async def _db() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    async def _user() -> UserContext:
        return UserContext(
            user_id=_USER,
            email="test@example.com",
            orgs=[OrgInToken(org_id=_ORG, slug="test-org", role=role)],
        )

    fa.dependency_overrides[get_db_conn] = _db
    fa.dependency_overrides[get_current_user] = _user
    return TestClient(fa, raise_server_exceptions=False)


def test_get_default_state_when_no_row():
    conn = mock.AsyncMock()
    conn.fetchrow = mock.AsyncMock(return_value=None)
    c = _client("viewer", conn)
    r = c.get(f"/api/v1/orgs/{_ORG}/autoheal/kill-switch")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "reason": None, "updated_at": None}


def test_get_reflects_enabled_row():
    conn = mock.AsyncMock()
    conn.fetchrow = mock.AsyncMock(
        return_value={"enabled": True, "reason": "incident-123", "updated_at": None}
    )
    c = _client("viewer", conn)
    r = c.get(f"/api/v1/orgs/{_ORG}/autoheal/kill-switch")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["reason"] == "incident-123"


def test_put_requires_admin_operator_forbidden():
    conn = mock.AsyncMock()
    c = _client("operator", conn)
    r = c.put(f"/api/v1/orgs/{_ORG}/autoheal/kill-switch", json={"enabled": True})
    assert r.status_code == 403
    conn.execute.assert_not_called()


def test_put_requires_admin_member_forbidden():
    conn = mock.AsyncMock()
    c = _client("member", conn)
    r = c.put(f"/api/v1/orgs/{_ORG}/autoheal/kill-switch", json={"enabled": True})
    assert r.status_code == 403


def test_put_admin_can_enable():
    conn = mock.AsyncMock()
    conn.execute = mock.AsyncMock()
    conn.fetchrow = mock.AsyncMock(
        return_value={"enabled": True, "reason": "drill", "updated_at": None}
    )
    c = _client("admin", conn)
    r = c.put(
        f"/api/v1/orgs/{_ORG}/autoheal/kill-switch",
        json={"enabled": True, "reason": "drill"},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args
    assert args[1] == "autoheal" and args[2] is True and args[3] == "drill"


def test_put_owner_can_disable():
    conn = mock.AsyncMock()
    conn.execute = mock.AsyncMock()
    conn.fetchrow = mock.AsyncMock(
        return_value={"enabled": False, "reason": None, "updated_at": None}
    )
    c = _client("owner", conn)
    r = c.put(f"/api/v1/orgs/{_ORG}/autoheal/kill-switch", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_no_auth_401():
    fa = FastAPI()
    fa.include_router(autoheal_router.router)

    async def _db() -> AsyncIterator[mock.AsyncMock]:
        yield mock.AsyncMock()

    fa.dependency_overrides[get_db_conn] = _db
    c = TestClient(fa, raise_server_exceptions=False)
    r = c.get(f"/api/v1/orgs/{_ORG}/autoheal/kill-switch")
    assert r.status_code == 401
