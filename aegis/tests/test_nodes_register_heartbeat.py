"""Tests for node registration + heartbeat (audit P1 #8).

Register was dead code (mismatched dispatcher signature + non-existent omodul);
there was no heartbeat at all, so node status had no real signal. These verify
the SQL upsert mints/withholds the agent_token correctly, heartbeat validates the
token and refreshes liveness, and status is derived from last_seen.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import nodes as nodes_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.models.node import derive_node_status

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_NODE = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _user(role: str = "admin") -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        email="t@x.com",
        orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)],
    )


def _client(conn: mock.AsyncMock, role: str = "admin") -> TestClient:
    app = FastAPI()
    app.include_router(nodes_router.router)
    app.dependency_overrides[get_current_user] = lambda: _user(role)

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def test_register_returns_token_on_first_insert():
    from aegis.server.lib.tokens import hash_token

    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"node_id": _NODE, "inserted": True}
    client = _client(conn)
    r = client.post(
        f"/api/v1/orgs/{_ORG}/nodes/register",
        json={
            "host": "10.0.0.5",
            "node_label": "edge-1",
            "ssh_username": "ops",
            "docker_tcp_port": 2375,
        },
    )
    assert r.status_code == 200
    body = r.json()
    tok = body["agent_token"]
    assert isinstance(tok, str) and tok and body["status"] == "registered"
    # what was stored is the HASH, not the plaintext returned to the caller
    stored = conn.fetchrow.await_args.args[6]
    assert stored == hash_token(tok) and stored != tok


def test_register_hides_token_on_reregister():
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"node_id": _NODE, "inserted": False}
    client = _client(conn)
    r = client.post(
        f"/api/v1/orgs/{_ORG}/nodes/register",
        json={"host": "10.0.0.5", "node_label": "edge-1", "ssh_username": "ops"},
    )
    assert r.status_code == 200
    assert r.json()["agent_token"] is None and r.json()["status"] == "updated"


def test_heartbeat_rejects_bad_token():
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"agent_token": "real-token"}
    client = _client(conn)
    r = client.post(
        f"/api/v1/orgs/{_ORG}/nodes/{_NODE}/heartbeat",
        json={"agent_token": "wrong-token"},
    )
    assert r.status_code == 401
    conn.execute.assert_not_awaited()  # liveness not refreshed


def test_heartbeat_accepts_valid_token_and_updates_last_seen():
    from aegis.server.lib.tokens import hash_token

    conn = mock.AsyncMock()
    # DB stores the hash; the agent presents the plaintext.
    conn.fetchrow.return_value = {"agent_token": hash_token("real-token")}
    client = _client(conn)
    r = client.post(
        f"/api/v1/orgs/{_ORG}/nodes/{_NODE}/heartbeat",
        json={"agent_token": "real-token", "cpus": 8},
    )
    assert r.status_code == 200 and r.json()["status"] == "online"
    sql = conn.execute.await_args.args[0]
    assert "last_seen = now()" in sql


def test_heartbeat_404_when_node_missing():
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = None
    client = _client(conn)
    r = client.post(
        f"/api/v1/orgs/{_ORG}/nodes/{_NODE}/heartbeat",
        json={"agent_token": "x"},
    )
    assert r.status_code == 404


def test_derive_node_status_thresholds():
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert derive_node_status(None, now=now) == "offline"
    assert derive_node_status(now - timedelta(seconds=30), now=now) == "online"
    assert derive_node_status(now - timedelta(seconds=120), now=now) == "stale"
    assert derive_node_status(now - timedelta(seconds=600), now=now) == "offline"
