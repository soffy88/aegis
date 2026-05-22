"""Tests for events HTTP API."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import alerts, events


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(events.router)
    fa.include_router(alerts.router)

    new_id = uuid.uuid4()

    async def _fake_conn() -> AsyncIterator[mock.AsyncMock]:
        m = mock.AsyncMock()
        m.fetchrow.return_value = {"id": str(new_id)}
        m.fetch.return_value = []
        yield m

    fa.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(fa) as c:
        yield c


class TestEventsApi:
    def test_create_event(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/events",
            json={
                "event_type": "user_action",
                "payload": {"action": "click"},
            },
        )
        assert r.status_code == 201
        assert "id" in r.json()

    def test_list_events_empty(self, client: TestClient) -> None:
        r = client.get("/api/v1/events")
        assert r.status_code == 200
        assert r.json() == []

    def test_invalid_org_header(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/events",
            headers={"X-Org-Id": "not-a-uuid"},
            json={"event_type": "x"},
        )
        assert r.status_code == 400


class TestAlertsApi:
    def test_ingest_alert(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/alerts/ingest",
            json={
                "alert_name": "rabbitmq.connection_reset",
                "severity": "critical",
            },
        )
        assert r.status_code == 202
        body = r.json()
        assert "trace_id" in body
        assert body["trace_id"].startswith("trc_")
        assert "brain_pipeline" in body
