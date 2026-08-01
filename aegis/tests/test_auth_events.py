"""Account-level auth event trail (A3) — login/logout/password events.

DB-free: the auth router is exercised with a mocked asyncpg connection and mocked
repositories, so these assert the wiring (which events get written, with what
fields) rather than Postgres behaviour.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest import mock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import auth as auth_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.persistence.auth_events import (
    LOGIN_FAILED,
    LOGIN_SUCCEEDED,
    client_info,
    record_auth_event,
)

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _client(conn: mock.AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router.router)

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=_USER, email="t@x.com", orgs=[OrgInToken(org_id=_ORG, slug="o", role="owner")]
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth_event_calls(conn: mock.AsyncMock) -> list[tuple]:
    return [
        c.args
        for c in conn.execute.await_args_list
        if c.args and "INSERT INTO auth_events" in c.args[0]
    ]


# ── record_auth_event / client_info ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_auth_event_writes_row() -> None:
    conn = mock.AsyncMock()
    await record_auth_event(
        conn, event=LOGIN_SUCCEEDED, user_id=_USER, email="t@x.com", detail="d"
    )
    args = _auth_event_calls(conn)[0]
    assert args[1:] == (_USER, "t@x.com", LOGIN_SUCCEEDED, None, None, "d")


@pytest.mark.asyncio
async def test_record_auth_event_never_raises() -> None:
    """Auditing must not be able to break signing in."""
    conn = mock.AsyncMock()
    conn.execute.side_effect = RuntimeError("db down")
    await record_auth_event(conn, event=LOGIN_FAILED, email="t@x.com")  # must not raise


def test_client_info_prefers_forwarded_for() -> None:
    """Aegis sits behind Caddy/cloudflared — without XFF every event would record
    the proxy address instead of the real client."""
    scope = {
        "type": "http",
        "headers": [
            (b"x-forwarded-for", b"203.0.113.7, 10.0.0.1"),
            (b"user-agent", b"curl/8"),
        ],
        "client": ("10.0.0.1", 1234),
    }
    assert client_info(Request(scope)) == ("203.0.113.7", "curl/8")


def test_client_info_none_request() -> None:
    assert client_info(None) == (None, None)


# ── router wiring ────────────────────────────────────────────────────────────


def test_login_unknown_email_records_failure() -> None:
    conn = mock.AsyncMock()
    with mock.patch.object(auth_router, "UserRepository") as repo:
        repo.return_value.get_by_email = mock.AsyncMock(return_value=None)
        r = _client(conn).post("/api/v1/auth/login", json={"email": "no@x.com", "password": "p"})
    assert r.status_code == 401
    events = _auth_event_calls(conn)
    assert len(events) == 1
    assert events[0][3] == LOGIN_FAILED
    assert events[0][2] == "no@x.com"  # email recorded even with no matching user


def test_login_bad_password_records_failure_with_user_id() -> None:
    conn = mock.AsyncMock()
    user = mock.MagicMock(id=_USER, email="t@x.com", is_active=True, password_hash="h")
    with (
        mock.patch.object(auth_router, "UserRepository") as repo,
        mock.patch.object(auth_router, "argon2_verify", return_value=False),
    ):
        repo.return_value.get_by_email = mock.AsyncMock(return_value=user)
        r = _client(conn).post("/api/v1/auth/login", json={"email": "t@x.com", "password": "bad"})
    assert r.status_code == 401
    events = _auth_event_calls(conn)
    assert events[0][3] == LOGIN_FAILED
    assert events[0][1] == _USER
    assert events[0][6] == "bad password"


def test_login_success_records_event() -> None:
    conn = mock.AsyncMock()
    user = mock.MagicMock(
        id=_USER, email="t@x.com", is_active=True, password_hash="h", token_epoch=0
    )
    with (
        mock.patch.object(auth_router, "UserRepository") as repo,
        mock.patch.object(auth_router, "MembershipRepository") as memb,
        mock.patch.object(auth_router, "OrgRepository") as orgs,
        mock.patch.object(auth_router, "argon2_verify", return_value=True),
    ):
        repo.return_value.get_by_email = mock.AsyncMock(return_value=user)
        repo.return_value.update_last_login = mock.AsyncMock()
        memb.return_value.list_by_user = mock.AsyncMock(return_value=[])
        orgs.return_value.get_by_id = mock.AsyncMock(return_value=None)
        r = _client(conn).post("/api/v1/auth/login", json={"email": "t@x.com", "password": "ok"})
    assert r.status_code == 200
    assert _auth_event_calls(conn)[0][3] == LOGIN_SUCCEEDED


def test_events_endpoint_is_self_scoped() -> None:
    """The trail is account-level: it may only ever return the caller's own rows."""
    conn = mock.AsyncMock()
    conn.fetch.return_value = []
    r = _client(conn).get("/api/v1/auth/events")
    assert r.status_code == 200
    sql, user_id, limit = conn.fetch.await_args.args
    assert "FROM auth_events" in sql
    assert user_id == _USER
    assert limit == 50
