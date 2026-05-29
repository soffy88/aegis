"""Tests for health endpoint — GET /api/v1/health (公开, 不带 org)."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.routers import health


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(health.router)
    with TestClient(fa) as c:
        yield c


class TestHealth:
    def test_liveness(self, client: TestClient) -> None:
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
