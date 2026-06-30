"""Tests for the audit log helper + read endpoint."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import audit as audit_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.persistence.audit import record_audit

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ACTOR = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_NOW = datetime(2026, 6, 1, tzinfo=UTC)


# ── helper ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_audit_inserts() -> None:
    conn = mock.AsyncMock()
    await record_audit(
        conn, org_id=_ORG, actor_user_id=_ACTOR, action="member.role_changed",
        target_type="user", target_id="x", metadata={"to": "admin"},
    )
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO audit_log" in sql
    assert conn.execute.await_args.args[3] == "member.role_changed"


@pytest.mark.asyncio
async def test_record_audit_swallows_errors() -> None:
    conn = mock.AsyncMock()
    conn.execute.side_effect = RuntimeError("db down")
    # must NOT raise — auditing is best-effort
    await record_audit(conn, org_id=_ORG, action="x")


# ── read endpoint ────────────────────────────────────────────────────────────────


def _user(role: str) -> UserContext:
    return UserContext(
        user_id=_ACTOR, email="a@x.com",
        orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)],
    )


@pytest.fixture
def conn() -> mock.AsyncMock:
    return mock.AsyncMock()


def _client(conn: mock.AsyncMock, role: str) -> TestClient:
    app = FastAPI()
    app.include_router(audit_router.router)
    app.dependency_overrides[get_current_user] = lambda: _user(role)

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def test_admin_can_read_audit_log(conn: mock.AsyncMock) -> None:
    conn.fetch.return_value = [
        {"id": uuid.uuid4(), "org_id": _ORG, "actor_user_id": _ACTOR,
         "action": "member.added", "target_type": "user", "target_id": "x",
         "metadata": {}, "created_at": _NOW}
    ]
    r = _client(conn, "admin").get(f"/api/v1/orgs/{_ORG}/audit-log")
    assert r.status_code == 200
    assert r.json()[0]["action"] == "member.added"


def test_member_cannot_read_audit_log(conn: mock.AsyncMock) -> None:
    # VIEW_AUDIT_LOG is admin+; member must be forbidden.
    r = _client(conn, "member").get(f"/api/v1/orgs/{_ORG}/audit-log")
    assert r.status_code == 403


def test_action_filter_passed_through(conn: mock.AsyncMock) -> None:
    conn.fetch.return_value = []
    _client(conn, "owner").get(f"/api/v1/orgs/{_ORG}/audit-log?action=member.removed")
    # action filter becomes a bound param
    assert "member.removed" in conn.fetch.await_args.args
