"""API test: uptime-targets list exposes last_tls_days_remaining (§3.2)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import uptime_targets as uptime_targets_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def test_list_targets_includes_tls_days_remaining():
    conn = mock.AsyncMock()
    conn.fetch = mock.AsyncMock(
        return_value=[
            {
                "id": uuid.uuid4(),
                "name": "web",
                "url": "https://example.com/health",
                "interval_seconds": 60,
                "expected_status": 200,
                "enabled": True,
                "last_up": True,
                "last_latency_ms": 42,
                "last_checked_at": datetime(2026, 6, 1, tzinfo=UTC),
                "last_error": None,
                "last_tls_days_remaining": 55.0,
            }
        ]
    )

    fa = FastAPI()
    fa.include_router(uptime_targets_router.router)

    async def _db() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    async def _user() -> UserContext:
        return UserContext(
            user_id=_USER,
            email="test@example.com",
            orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="viewer")],
        )

    fa.dependency_overrides[get_db_conn] = _db
    fa.dependency_overrides[get_current_user] = _user
    c = TestClient(fa, raise_server_exceptions=False)

    r = c.get(f"/api/v1/orgs/{_ORG}/uptime-targets")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["last_tls_days_remaining"] == 55.0

    sql = conn.fetch.await_args.args[0]
    assert "last_tls_days_remaining" in sql
