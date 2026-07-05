"""Tests for GET /api/v1/metrics/top-series — which container holds the current max."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import metrics as metrics_router
from aegis.server.auth.dependencies import UserContext, get_current_user


def _client(conn: mock.AsyncMock) -> TestClient:
    fa = FastAPI()
    fa.include_router(metrics_router.router)

    async def _db() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    async def _user() -> UserContext:
        return UserContext(user_id=uuid.uuid4(), email="test@example.com", orgs=[])

    fa.dependency_overrides[get_db_conn] = _db
    fa.dependency_overrides[get_current_user] = _user
    return TestClient(fa, raise_server_exceptions=False)


def _row(name: str, image: str, value: float, ts: datetime) -> dict:
    return {"name": name, "image": image, "value": value, "ts": ts}


def test_returns_series_with_highest_value():
    ts = datetime(2026, 7, 5, tzinfo=UTC)
    conn = mock.AsyncMock()
    conn.fetch = mock.AsyncMock(
        return_value=[
            _row("aaa", "quant-qlib-v2:latest", 89.9, ts),
            _row("bbb", "timescaledb:pg16", 52.0, ts),
        ]
    )
    r = _client(conn).get("/api/v1/metrics/top-series?metric_name=container_cpu_percent")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["image"] == "quant-qlib-v2:latest"
    assert body[0]["value"] == 89.9


def test_limit_returns_top_n_sorted_desc():
    ts = datetime(2026, 7, 5, tzinfo=UTC)
    conn = mock.AsyncMock()
    conn.fetch = mock.AsyncMock(
        return_value=[
            _row("aaa", "low:latest", 10.0, ts),
            _row("bbb", "high:latest", 90.0, ts),
            _row("ccc", "mid:latest", 50.0, ts),
        ]
    )
    r = _client(conn).get("/api/v1/metrics/top-series?metric_name=container_cpu_percent&limit=2")
    body = r.json()
    assert [b["image"] for b in body] == ["high:latest", "mid:latest"]


def test_query_excludes_cadvisor_root_and_nameless_rows():
    """SQL WHERE clause itself excludes id='/' and name IS NULL — assert the
    query we send encodes that (the fake conn can't apply real SQL semantics)."""
    conn = mock.AsyncMock()
    conn.fetch = mock.AsyncMock(return_value=[])
    _client(conn).get("/api/v1/metrics/top-series?metric_name=container_memory_working_set_bytes")
    sql = conn.fetch.await_args.args[0]
    assert "tags->>'id' IS NULL OR tags->>'id' != '/'" in sql
    assert "tags->>'name' IS NOT NULL" in sql


def test_empty_result_when_no_recent_samples():
    conn = mock.AsyncMock()
    conn.fetch = mock.AsyncMock(return_value=[])
    r = _client(conn).get("/api/v1/metrics/top-series?metric_name=container_cpu_percent")
    assert r.status_code == 200
    assert r.json() == []


def test_no_auth_401():
    fa = FastAPI()
    fa.include_router(metrics_router.router)

    async def _db() -> AsyncIterator[mock.AsyncMock]:
        yield mock.AsyncMock()

    fa.dependency_overrides[get_db_conn] = _db
    c = TestClient(fa, raise_server_exceptions=False)
    r = c.get("/api/v1/metrics/top-series?metric_name=container_cpu_percent")
    assert r.status_code == 401
