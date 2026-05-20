"""Tests for health endpoints (uses TestClient with overridden DB dep)."""
from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import alerts, events, health


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """TestClient with DB dep overridden."""
    fa = FastAPI()
    fa.include_router(health.router)
    fa.include_router(events.router)
    fa.include_router(alerts.router)

    async def _fake_conn() -> AsyncIterator[mock.AsyncMock]:
        m = mock.AsyncMock()
        m.fetchrow.return_value = {"ok": 1, "id": "00000000-0000-0000-0000-000000000099"}
        m.fetch.return_value = []
        yield m

    fa.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(fa) as c:
        yield c


class TestHealth:
    def test_liveness(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "aegis"

    def test_readiness(self, client: TestClient) -> None:
        r = client.get("/ready")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
