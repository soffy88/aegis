"""Tests for POST /api/v1/metrics/ingest."""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import metrics as metrics_router
from aegis.server.runtime.config import AegisSettings, get_settings

_PAYLOAD = {
    "hostname": "prod-host-01",
    "collected_at": "2026-06-06T00:00:00Z",
    "metrics": [
        {"name": "cpu_percent", "value": 42.5, "unit": "%"},
        {"name": "ram_percent", "value": 67.1, "unit": "%"},
    ],
}


def _make_app(agent_token: str = "") -> FastAPI:
    cfg = AegisSettings(agent_token=agent_token)  # type: ignore[call-arg]
    fa = FastAPI()
    fa.include_router(metrics_router.router)

    async def _fake_conn() -> AsyncIterator[mock.AsyncMock]:
        m = mock.AsyncMock()
        m.executemany = mock.AsyncMock()
        yield m

    fa.dependency_overrides[get_db_conn] = _fake_conn
    fa.dependency_overrides[get_settings] = lambda: cfg
    return fa


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(_make_app()) as c:
        yield c


@pytest.fixture
def client_with_token() -> Generator[TestClient, None, None]:
    with TestClient(_make_app(agent_token="secret-token")) as c:
        yield c


class TestMetricsIngest:
    def test_ingest_no_auth_when_token_empty(self, client: TestClient) -> None:
        """No agent_token configured → any request accepted without auth."""
        r = client.post("/api/v1/metrics/ingest", json=_PAYLOAD)
        assert r.status_code == 202
        assert r.json()["accepted"] == 2
        assert r.json()["hostname"] == "prod-host-01"

    def test_ingest_valid_token(self, client_with_token: TestClient) -> None:
        r = client_with_token.post(
            "/api/v1/metrics/ingest",
            json=_PAYLOAD,
            headers={"Authorization": "Bearer secret-token"},
        )
        assert r.status_code == 202
        assert r.json()["accepted"] == 2

    def test_ingest_wrong_token_returns_401(self, client_with_token: TestClient) -> None:
        r = client_with_token.post(
            "/api/v1/metrics/ingest",
            json=_PAYLOAD,
            headers={"Authorization": "Bearer wrong"},
        )
        assert r.status_code == 401

    def test_ingest_missing_auth_header_returns_401(self, client_with_token: TestClient) -> None:
        r = client_with_token.post("/api/v1/metrics/ingest", json=_PAYLOAD)
        assert r.status_code == 401

    def test_ingest_empty_metrics_returns_zero(self, client: TestClient) -> None:
        payload = {**_PAYLOAD, "metrics": []}
        r = client.post("/api/v1/metrics/ingest", json=payload)
        assert r.status_code == 202
        assert r.json()["accepted"] == 0

    def test_ingest_stores_all_metric_points(self, client: TestClient) -> None:
        payload = {
            **_PAYLOAD,
            "metrics": [
                {"name": "cpu_percent", "value": 10.0},
                {"name": "ram_percent", "value": 20.0},
                {"name": "disk_percent", "value": 30.0},
            ],
        }
        r = client.post("/api/v1/metrics/ingest", json=payload)
        assert r.status_code == 202
        assert r.json()["accepted"] == 3

    def test_ingest_metric_with_tags(self, client: TestClient) -> None:
        payload = {
            **_PAYLOAD,
            "metrics": [
                {
                    "name": "docker_cpu_percent",
                    "value": 5.5,
                    "unit": "%",
                    "tags": {"container": "postgres"},
                }
            ],
        }
        r = client.post("/api/v1/metrics/ingest", json=payload)
        assert r.status_code == 202
        assert r.json()["accepted"] == 1
