"""Tests for self-serve uptime-target + autoheal-policy CRUD routers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import autoheal_policies as ah
from aegis.server.api.routers import uptime_targets as up
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _client(router, role="owner"):
    conn = mock.AsyncMock()
    fa = FastAPI()
    fa.include_router(router)

    async def _user():
        return UserContext(user_id=uuid.uuid4(), email="t@x.com",
                           orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)])

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_current_user] = _user
    fa.dependency_overrides[get_db_conn] = _conn
    return TestClient(fa, raise_server_exceptions=False), conn


def test_uptime_create_and_list():
    c, conn = _client(up.router)
    conn.fetchrow.return_value = {"id": uuid.uuid4(), "name": "svc", "url": "http://x/health",
                                  "interval_seconds": 60, "expected_status": 200, "enabled": True}
    r = c.post(f"/api/v1/orgs/{_ORG}/uptime-targets",
               json={"name": "svc", "url": "http://x/health"})
    assert r.status_code == 201 and r.json()["name"] == "svc"
    conn.fetch.return_value = []
    assert c.get(f"/api/v1/orgs/{_ORG}/uptime-targets").status_code == 200


def test_uptime_rejects_bad_url():
    c, _ = _client(up.router)
    r = c.post(f"/api/v1/orgs/{_ORG}/uptime-targets", json={"name": "x", "url": "ftp://x"})
    assert r.status_code == 422  # validator


def test_uptime_viewer_cannot_create():
    c, _ = _client(up.router, role="viewer")
    r = c.post(f"/api/v1/orgs/{_ORG}/uptime-targets", json={"name": "x", "url": "http://x"})
    assert r.status_code == 403


def test_autoheal_policy_create():
    c, conn = _client(ah.router)
    conn.fetchrow.return_value = {"id": uuid.uuid4(), "name": "p", "target_container": "web",
        "trigger_metric": "probe_up", "trigger_operator": "<", "trigger_threshold": 1.0,
        "action": "restart", "dry_run": True, "cooldown_seconds": 300, "enabled": True,
        "last_triggered_at": None}
    r = c.post(f"/api/v1/orgs/{_ORG}/autoheal-policies",
               json={"name": "p", "target_container": "web", "trigger_metric": "probe_up",
                     "trigger_threshold": 1.0})
    assert r.status_code == 201 and r.json()["dry_run"] is True


def test_autoheal_delete_404():
    c, conn = _client(ah.router)
    conn.execute.return_value = "DELETE 0"
    r = c.delete(f"/api/v1/orgs/{_ORG}/autoheal-policies/{uuid.uuid4()}")
    assert r.status_code == 404
