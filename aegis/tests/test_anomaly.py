"""Tests for EWMA anomaly detection + scan + endpoints."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import metrics as metrics_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.services.anomaly import ewma_anomaly
from aegis.server.services.anomaly_scan import scan_anomalies

_STABLE = [20, 21, 19, 20, 22, 20, 21, 19, 20, 21.0]
_SPIKE = [20, 21, 19, 20, 22, 20, 21, 19, 20, 65.0]


# ── detector ─────────────────────────────────────────────────────────────────────


def test_stable_series_not_anomalous() -> None:
    r = ewma_anomaly(_STABLE)
    assert r is not None and r.is_anomaly is False


def test_spike_is_anomalous() -> None:
    r = ewma_anomaly(_SPIKE)
    assert r is not None and r.is_anomaly is True and r.score > 3


def test_insufficient_samples_returns_none() -> None:
    assert ewma_anomaly([1, 2, 3]) is None


# ── scan ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_inserts_anomaly_with_cooldown_respected() -> None:
    conn = mock.AsyncMock()
    # one active series
    conn.fetch.side_effect = [
        [{"hostname": "h1", "metric_name": "cpu"}],          # distinct series
        [{"value": v} for v in _SPIKE],                       # its values
    ]
    conn.fetchval.return_value = None  # no recent anomaly → not in cooldown
    found = await scan_anomalies(conn)
    assert found == 1
    assert any("INSERT INTO metric_anomalies" in c.args[0] for c in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_scan_skips_when_in_cooldown() -> None:
    conn = mock.AsyncMock()
    conn.fetch.side_effect = [
        [{"hostname": "h1", "metric_name": "cpu"}],
        [{"value": v} for v in _SPIKE],
    ]
    conn.fetchval.return_value = 1  # recent anomaly exists → cooldown
    found = await scan_anomalies(conn)
    assert found == 0
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_scan_skips_stable_series() -> None:
    conn = mock.AsyncMock()
    conn.fetch.side_effect = [
        [{"hostname": "h1", "metric_name": "cpu"}],
        [{"value": v} for v in _STABLE],
    ]
    found = await scan_anomalies(conn)
    assert found == 0


# ── endpoints ────────────────────────────────────────────────────────────────────


def _client(conn: mock.AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(metrics_router.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=uuid.uuid4(), email="a@x.com",
        orgs=[OrgInToken(org_id=uuid.uuid4(), slug="o", role="viewer")],
    )

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def test_anomaly_endpoint_flags_spike() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"value": v} for v in _SPIKE]
    r = _client(conn).get("/api/v1/metrics/anomaly?metric_name=cpu")
    assert r.status_code == 200
    body = r.json()
    assert body["evaluated"] is True and body["is_anomaly"] is True


def test_anomaly_endpoint_not_enough_samples() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"value": 1}, {"value": 2}]
    r = _client(conn).get("/api/v1/metrics/anomaly?metric_name=cpu")
    assert r.json()["evaluated"] is False
