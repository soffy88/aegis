"""E2e tests for POST /api/{project_id}/envelope/ — RUN_SMOKE=1."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import asyncpg
import httpx
import pytest

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
SMOKE_SKIP = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_ORG_ID = uuid.UUID("c6060001-0000-0000-0000-000000000000")
_USER_ID = uuid.UUID("c6060003-0000-0000-0000-000000000000")

_EH = json.dumps({"sent_at": "2026-06-01T00:00:00Z"})
_IH = json.dumps({"type": "event"})

_EVENT = {
    "event_id": "aabb0001",
    "level": "error",
    "exception": {
        "values": [
            {
                "type": "RuntimeError",
                "value": "database unavailable",
                "stacktrace": {"frames": [{"function": "connect_db", "filename": "/app/db.py"}]},
            }
        ]
    },
}


def _make_envelope(*payloads: dict) -> bytes:  # type: ignore[type-arg]
    parts = [_EH]
    for p in payloads:
        parts += [_IH, json.dumps(p)]
    return ("\n".join(parts) + "\n").encode()


def _sentry_auth(public_key: str) -> str:
    return (
        f"Sentry sentry_version=7, sentry_key={public_key},"
        " sentry_client=sentry.python/2.0.0, sentry_timestamp=1234567890"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container_env() -> Generator[Any, None, None]:
    if not RUN_SMOKE:
        pytest.skip("set RUN_SMOKE=1 to run")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("timescale/timescaledb:2.26.3-pg18") as pg:
        yield pg


@pytest.fixture(scope="module")
def env_dsn(pg_container_env: Any) -> str:
    return pg_container_env.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
async def env_conn(env_dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:
    from aegis.server.persistence.migrations import apply_migrations

    conn: asyncpg.Connection = await asyncpg.connect(env_dsn)
    await apply_migrations(conn)
    await conn.execute(
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'env-test', 'ENV', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG_ID,
    )
    await conn.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1,'env@t.com','h')"
        " ON CONFLICT DO NOTHING",
        _USER_ID,
    )
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def project_with_key(env_conn: asyncpg.Connection) -> dict:
    """Create a test project and return its id + sentry_public_key."""
    proj_id = uuid.uuid4()
    suffix = proj_id.hex[:8]
    await env_conn.execute(
        "INSERT INTO projects (id, org_id, name, slug, display_name)"
        " VALUES ($1, $2, $3, $4, 'Env Proj')",
        proj_id,
        _ORG_ID,
        f"env-proj-{suffix}",
        f"env-proj-{suffix}",
    )
    row = await env_conn.fetchrow("SELECT sentry_public_key FROM projects WHERE id = $1", proj_id)
    return {"project_id": proj_id, "public_key": row["sentry_public_key"]}


@pytest.fixture(scope="module")
def envelope_app() -> Any:
    from aegis.server.runtime.config import get_settings

    get_settings.cache_clear()
    from fastapi import FastAPI

    from aegis.server.api.routers.envelope import router as envelope_router

    app = FastAPI()
    app.include_router(envelope_router)
    return app


@pytest.fixture
async def env_client(
    envelope_app: Any, env_conn: asyncpg.Connection
) -> AsyncGenerator[httpx.AsyncClient, None]:
    from aegis.server.api.deps import get_db_conn

    async def _override() -> AsyncGenerator[asyncpg.Connection, None]:
        yield env_conn

    envelope_app.dependency_overrides[get_db_conn] = _override
    transport = httpx.ASGITransport(app=envelope_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    envelope_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@SMOKE_SKIP
class TestEnvelopeEndpoint:
    async def test_envelope_endpoint_success(
        self, env_client: httpx.AsyncClient, project_with_key: dict
    ) -> None:
        pid = project_with_key["project_id"]
        key = project_with_key["public_key"]
        raw = _make_envelope(_EVENT)
        resp = await env_client.post(
            f"/api/{pid}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": _sentry_auth(key)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["id"] is not None
        assert len(data["id"]) == 32  # hex UUID, no dashes

    async def test_envelope_endpoint_returns_event_id_hex(
        self, env_client: httpx.AsyncClient, project_with_key: dict
    ) -> None:
        pid = project_with_key["project_id"]
        key = project_with_key["public_key"]
        raw = _make_envelope(_EVENT)
        resp = await env_client.post(
            f"/api/{pid}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": _sentry_auth(key)},
        )
        assert resp.status_code == 200
        event_hex = resp.json()["id"]
        assert "-" not in event_hex
        uuid.UUID(hex=event_hex)  # validates it's a proper UUID hex

    async def test_envelope_endpoint_wrong_public_key(
        self, env_client: httpx.AsyncClient, project_with_key: dict
    ) -> None:
        pid = project_with_key["project_id"]
        raw = _make_envelope(_EVENT)
        resp = await env_client.post(
            f"/api/{pid}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": _sentry_auth("deadbeefdeadbeefdeadbeefdeadbeef")},
        )
        assert resp.status_code == 403

    async def test_envelope_endpoint_wrong_project_id(
        self, env_client: httpx.AsyncClient, project_with_key: dict
    ) -> None:
        key = project_with_key["public_key"]
        wrong_id = uuid.uuid4()
        raw = _make_envelope(_EVENT)
        resp = await env_client.post(
            f"/api/{wrong_id}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": _sentry_auth(key)},
        )
        assert resp.status_code == 403

    async def test_envelope_endpoint_invalid_sentry_auth_header(
        self, env_client: httpx.AsyncClient, project_with_key: dict
    ) -> None:
        pid = project_with_key["project_id"]
        raw = _make_envelope(_EVENT)
        resp = await env_client.post(
            f"/api/{pid}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": "not-a-valid-header"},
        )
        assert resp.status_code == 400

    async def test_envelope_endpoint_missing_sentry_auth_header(
        self, env_client: httpx.AsyncClient, project_with_key: dict
    ) -> None:
        pid = project_with_key["project_id"]
        raw = _make_envelope(_EVENT)
        resp = await env_client.post(f"/api/{pid}/envelope/", content=raw)
        assert resp.status_code in (400, 422)

    async def test_envelope_endpoint_empty_body(
        self, env_client: httpx.AsyncClient, project_with_key: dict
    ) -> None:
        pid = project_with_key["project_id"]
        key = project_with_key["public_key"]
        resp = await env_client.post(
            f"/api/{pid}/envelope/",
            content=b"",
            headers={"X-Sentry-Auth": _sentry_auth(key)},
        )
        assert resp.status_code == 400

    async def test_envelope_endpoint_multi_event_envelope(
        self, env_client: httpx.AsyncClient, project_with_key: dict, env_conn: asyncpg.Connection
    ) -> None:
        pid = project_with_key["project_id"]
        key = project_with_key["public_key"]
        ev2 = {
            **_EVENT,
            "event_id": "aabb0002",
            "exception": {"values": [{"type": "ValueError", "value": "bad input"}]},
        }
        raw = _make_envelope(_EVENT, ev2)
        resp = await env_client.post(
            f"/api/{pid}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": _sentry_auth(key)},
        )
        assert resp.status_code == 200
        # First event's id is returned
        assert resp.json()["id"] is not None

    async def test_envelope_endpoint_message_only_event(
        self, env_client: httpx.AsyncClient, project_with_key: dict
    ) -> None:
        pid = project_with_key["project_id"]
        key = project_with_key["public_key"]
        msg_event = {"event_id": "aabb0003", "level": "info", "message": "user logged in"}
        raw = _make_envelope(msg_event)
        resp = await env_client.post(
            f"/api/{pid}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": _sentry_auth(key)},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] is not None

    async def test_envelope_endpoint_existing_issue_aggregates(
        self, env_client: httpx.AsyncClient, project_with_key: dict, env_conn: asyncpg.Connection
    ) -> None:
        """Second identical event → issue event_count becomes 2, not a new issue."""
        pid = project_with_key["project_id"]
        key = project_with_key["public_key"]
        raw = _make_envelope(_EVENT)

        # First event
        r1 = await env_client.post(
            f"/api/{pid}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": _sentry_auth(key)},
        )
        assert r1.status_code == 200

        # Second identical event
        r2 = await env_client.post(
            f"/api/{pid}/envelope/",
            content=raw,
            headers={"X-Sentry-Auth": _sentry_auth(key)},
        )
        assert r2.status_code == 200

        # Verify issue event_count == 2
        rows = await env_conn.fetch(
            "SELECT event_count FROM error_issues WHERE project_id = $1 AND exception_type = $2",
            pid,
            "RuntimeError",
        )
        counts = [r["event_count"] for r in rows]
        assert any(c >= 2 for c in counts)
