"""Tests for the telemetry router — org-scoped ingest + query.

Regression coverage for a confirmed cross-tenant data leak: aegis_spans /
aegis_rum had no org_id column at all, ingest wrote to a fixed org-less URL,
and every query endpoint under /api/v1/orgs/{org_id}/telemetry did an
un-scoped WHERE — so any viewer in ANY org could read every other org's
traces/RUM. Fix: org-scoped ingest paths that stamp org_id, plus org_id in
every query's WHERE clause (legacy NULL-org rows become invisible to
everyone rather than staying visible to everyone).

Fast tests (no DB) verify wiring: SQL text + bound params. RUN_SMOKE=1 tests
verify actual cross-org row isolation against a real Postgres.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from typing import Any
from unittest import mock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import telemetry as router_mod
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.runtime.config import get_settings

_ORG_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _user(org_id: uuid.UUID, role: str = "viewer") -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        email="t@example.com",
        orgs=[OrgInToken(org_id=org_id, slug="o", role=role)],
    )


def _mock_pool(conn: mock.AsyncMock) -> mock.MagicMock:
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


def _query_client(conn: mock.AsyncMock, org_id: uuid.UUID, role: str = "viewer") -> TestClient:
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: _user(org_id, role)

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def _ingest_client() -> TestClient:
    app = FastAPI()
    app.include_router(router_mod.ingest_router)
    return TestClient(app, raise_server_exceptions=False)


_TRACE_BODY = {
    "resourceSpans": [
        {
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
            "scopeSpans": [
                {
                    "spans": [
                        {
                            "traceId": "t1",
                            "spanId": "s1",
                            "name": "GET /",
                            "startTimeUnixNano": "1000",
                            "endTimeUnixNano": "2000",
                            "status": {"code": 0},
                        }
                    ]
                }
            ],
        }
    ]
}


# ===== ingest: org-scoped path replaces the old org-less one =====


def test_ingest_traces_old_orgless_path_removed() -> None:
    r = _ingest_client().post("/api/v1/telemetry/v1/traces", json={})
    assert r.status_code == 404


def test_ingest_rum_old_orgless_path_removed() -> None:
    r = _ingest_client().post("/api/v1/telemetry/rum", json={})
    assert r.status_code == 404


def test_ingest_traces_rejects_non_uuid_org_id() -> None:
    r = _ingest_client().post("/api/v1/telemetry/not-a-uuid/v1/traces", json=_TRACE_BODY)
    assert r.status_code == 422


def test_ingest_rum_rejects_non_uuid_org_id() -> None:
    r = _ingest_client().post("/api/v1/telemetry/not-a-uuid/rum", json={"app": "a", "page": "/"})
    assert r.status_code == 422


def test_ingest_traces_stamps_org_id(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = mock.AsyncMock()
    monkeypatch.setattr(router_mod, "get_pool", lambda: _mock_pool(conn))
    r = _ingest_client().post(f"/api/v1/telemetry/{_ORG_A}/v1/traces", json=_TRACE_BODY)
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    conn.executemany.assert_called_once()
    sql, rows = conn.executemany.call_args[0]
    assert "org_id" in sql
    assert rows[0][-1] == _ORG_A


# A payload shaped exactly like what an OTel Collector's otlphttp exporter emits
# with encoding=json (hex trace/span ids, parentSpanId, kind, string nanos). This
# pins the wire contract the collector + instrumented services rely on.
_OTLP_JSON_COLLECTOR_BODY = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "aegis-backend"}},
                    {"key": "host.name", "value": {"stringValue": "node-1"}},
                ]
            },
            "scopeSpans": [
                {
                    "scope": {"name": "opentelemetry.instrumentation.fastapi"},
                    "spans": [
                        {
                            "traceId": "5b8efff798038103d269b633813fc60c",
                            "spanId": "eee19b7ec3c1b174",
                            "parentSpanId": "eee19b7ec3c1b173",
                            "name": "GET /api/v1/health",
                            "kind": 2,
                            "startTimeUnixNano": "1700000000000000000",
                            "endTimeUnixNano": "1700000000012000000",
                            "status": {"code": 2},
                        }
                    ],
                }
            ],
        }
    ]
}


def test_ingest_parses_realistic_otlp_json_collector_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full field mapping for a collector-style OTLP/JSON span → aegis_spans row."""
    conn = mock.AsyncMock()
    monkeypatch.setattr(router_mod, "get_pool", lambda: _mock_pool(conn))
    r = _ingest_client().post(
        f"/api/v1/telemetry/{_ORG_A}/v1/traces", json=_OTLP_JSON_COLLECTOR_BODY
    )
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    _sql, rows = conn.executemany.call_args[0]
    (trace_id, span_id, parent, service, name, kind, start_ns, dur_ns, status_code, org) = rows[0]
    assert trace_id == "5b8efff798038103d269b633813fc60c"
    assert span_id == "eee19b7ec3c1b174"
    assert parent == "eee19b7ec3c1b173"
    assert service == "aegis-backend"  # from resource service.name
    assert name == "GET /api/v1/health"
    assert kind == 2
    assert start_ns == 1700000000000000000
    assert dur_ns == 12000000  # end - start
    assert status_code == 2
    assert org == _ORG_A


def test_ingest_rum_stamps_org_id(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = mock.AsyncMock()
    monkeypatch.setattr(router_mod, "get_pool", lambda: _mock_pool(conn))
    r = _ingest_client().post(
        f"/api/v1/telemetry/{_ORG_B}/rum",
        json={"app": "web", "page": "/x", "load_ms": 100},
    )
    assert r.status_code == 200
    conn.execute.assert_called_once()
    sql, *params = conn.execute.call_args[0]
    assert "org_id" in sql
    assert params[-1] == _ORG_B


def test_ingest_traces_still_enforces_shared_ingest_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """org_id in the path is only for attribution — the shared secret is still the auth."""
    get_settings.cache_clear()
    monkeypatch.setenv("AEGIS_TELEMETRY_INGEST_KEY", "topsecret")
    get_settings.cache_clear()
    try:
        r = _ingest_client().post(
            f"/api/v1/telemetry/{_ORG_A}/v1/traces",
            json=_TRACE_BODY,
            headers={"X-Aegis-Ingest-Key": "wrong"},
        )
        assert r.status_code == 401
    finally:
        monkeypatch.delenv("AEGIS_TELEMETRY_INGEST_KEY", raising=False)
        get_settings.cache_clear()


# ===== query router: every endpoint must scope its SQL by org_id =====


def test_rum_metrics_scopes_by_org() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = []
    r = _query_client(conn, _ORG_A).get(f"/api/v1/orgs/{_ORG_A}/telemetry/rum")
    assert r.status_code == 200
    sql, *params = conn.fetch.call_args[0]
    assert "org_id" in sql
    assert _ORG_A in params


def test_services_scopes_by_org() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = []
    r = _query_client(conn, _ORG_B).get(f"/api/v1/orgs/{_ORG_B}/telemetry/services")
    assert r.status_code == 200
    sql, *params = conn.fetch.call_args[0]
    assert "org_id" in sql
    assert _ORG_B in params


def test_traces_scopes_by_org() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = []
    r = _query_client(conn, _ORG_A).get(f"/api/v1/orgs/{_ORG_A}/telemetry/traces")
    assert r.status_code == 200
    sql, *params = conn.fetch.call_args[0]
    assert "org_id" in sql
    assert _ORG_A in params


def test_trace_detail_scopes_by_org() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [
        {
            "span_id": "s1",
            "parent_span_id": None,
            "service": "api",
            "name": "GET",
            "kind": 1,
            "start_ns": 0,
            "duration_ns": 10,
            "status_code": 0,
        }
    ]
    r = _query_client(conn, _ORG_A).get(f"/api/v1/orgs/{_ORG_A}/telemetry/traces/t1")
    assert r.status_code == 200
    sql, *params = conn.fetch.call_args[0]
    assert "org_id" in sql
    assert _ORG_A in params


def test_rca_scopes_both_queries_by_org() -> None:
    conn = mock.AsyncMock()
    conn.fetch.side_effect = [[], []]  # edges, then err
    r = _query_client(conn, _ORG_A).get(
        f"/api/v1/orgs/{_ORG_A}/telemetry/rca", params={"service": "api"}
    )
    assert r.status_code == 200
    assert conn.fetch.call_count == 2
    for call in conn.fetch.call_args_list:
        sql, *params = call[0]
        assert "org_id" in sql
        assert _ORG_A in params


def test_topology_scopes_both_queries_by_org() -> None:
    conn = mock.AsyncMock()
    conn.fetch.side_effect = [[], []]  # edges, then nodes
    r = _query_client(conn, _ORG_B).get(f"/api/v1/orgs/{_ORG_B}/telemetry/topology")
    assert r.status_code == 200
    assert conn.fetch.call_count == 2
    for call in conn.fetch.call_args_list:
        sql, *params = call[0]
        assert "org_id" in sql
        assert _ORG_B in params


# ===== real-DB regression: org A truly cannot see org B's (or legacy
# NULL-org) rows. Requires RUN_SMOKE=1 (spins up Postgres via testcontainers).
# =====

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
smoke = pytest.mark.skipif(
    not RUN_SMOKE, reason="set RUN_SMOKE=1 to run (spins up Postgres via testcontainers)"
)


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("timescale/timescaledb:2.26.3-pg18") as pg:
        yield pg


@pytest.fixture
async def pg_conn(pg_container: Any) -> AsyncGenerator[asyncpg.Connection, None]:
    dsn = pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    c = await asyncpg.connect(dsn)
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(c)
    try:
        yield c
    finally:
        await c.close()


async def _insert_span(
    conn: asyncpg.Connection, trace_id: str, service: str, org_id: uuid.UUID | None
) -> None:
    await conn.execute(
        """INSERT INTO aegis_spans
               (trace_id, span_id, parent_span_id, service, name, kind,
                start_ns, duration_ns, status_code, org_id)
           VALUES ($1, $2, NULL, $3, 'op', 0, 0, 10, 0, $4)""",
        trace_id,
        f"span-{trace_id}",
        service,
        org_id,
    )


@smoke
async def test_migration_adds_nullable_org_id_columns(pg_conn: asyncpg.Connection) -> None:
    for table in ("aegis_spans", "aegis_rum"):
        col = await pg_conn.fetchrow(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = 'org_id'",
            table,
        )
        assert col is not None, f"{table}.org_id missing"
        assert col["is_nullable"] == "YES"


@smoke
async def test_org_a_cannot_see_org_b_or_legacy_spans(pg_conn: asyncpg.Connection) -> None:
    await _insert_span(pg_conn, "tA", "svc-a", _ORG_A)
    await _insert_span(pg_conn, "tB", "svc-b", _ORG_B)
    await _insert_span(pg_conn, "tOld", "svc-old", None)  # pre-fix, un-attributed row

    result = await router_mod.services(
        org_id=_ORG_A, minutes=1440, conn=pg_conn, user=_user(_ORG_A)
    )
    seen = {r["service"] for r in result}
    assert seen == {"svc-a"}
    assert "svc-b" not in seen  # org B's data never leaks to org A
    assert "svc-old" not in seen  # legacy NULL-org rows: invisible to everyone now


@smoke
async def test_org_a_cannot_see_org_b_trace_detail(pg_conn: asyncpg.Connection) -> None:
    await _insert_span(pg_conn, "tShared", "svc-b-only", _ORG_B)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await router_mod.trace_detail(
            org_id=_ORG_A, trace_id="tShared", conn=pg_conn, user=_user(_ORG_A)
        )
    assert exc_info.value.status_code == 404

    # but org B itself can see it
    detail = await router_mod.trace_detail(
        org_id=_ORG_B, trace_id="tShared", conn=pg_conn, user=_user(_ORG_B)
    )
    assert detail["trace_id"] == "tShared"


@smoke
async def test_org_a_cannot_see_org_b_rum(pg_conn: asyncpg.Connection) -> None:
    await pg_conn.execute(
        "INSERT INTO aegis_rum (app, page, load_ms, org_id) VALUES ('appA', '/x', 100, $1)",
        _ORG_A,
    )
    await pg_conn.execute(
        "INSERT INTO aegis_rum (app, page, load_ms, org_id) VALUES ('appB', '/y', 200, $1)",
        _ORG_B,
    )
    await pg_conn.execute(
        "INSERT INTO aegis_rum (app, page, load_ms) VALUES ('appOld', '/z', 300)"
    )  # legacy row, org_id NULL

    result = await router_mod.rum_metrics(
        org_id=_ORG_A, minutes=1440, conn=pg_conn, user=_user(_ORG_A)
    )
    apps = {r["app"] for r in result}
    assert apps == {"appA"}


class _FakeRequest:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body


@smoke
async def test_ingest_traces_persists_org_id_end_to_end(
    pg_conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_mod, "get_pool", lambda: _mock_pool(pg_conn))
    result = await router_mod.ingest_traces(
        org_id=_ORG_A, request=_FakeRequest(_TRACE_BODY), x_aegis_ingest_key=None
    )
    assert result["accepted"] == 1
    row = await pg_conn.fetchrow("SELECT org_id FROM aegis_spans WHERE trace_id = 't1'")
    assert row["org_id"] == _ORG_A
