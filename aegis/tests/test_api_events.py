"""Tests for events + alerts HTTP API (C1-4 org-scoped paths)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import alerts, events
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="owner")],
    )


_EVENT_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

_EVENT_ROW: dict[str, object] = {
    "id": _EVENT_ID,
    "ts": "2026-06-06T00:00:00Z",
    "event_type": "service_down",
    "severity": "critical",
    "payload": {"service": "worker"},
    "omodul_kind": None,
    "autoheal_plugin": None,
    "trace_id": "trc_abc123",
}


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(events.router)
    fa.include_router(alerts.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    new_id = uuid.uuid4()

    async def _fake_conn() -> AsyncIterator[mock.AsyncMock]:
        m = mock.AsyncMock()
        m.fetchrow.return_value = {"id": str(new_id)}
        m.fetch.return_value = []
        yield m

    fa.dependency_overrides[get_db_conn] = _fake_conn
    with TestClient(fa) as c:
        yield c


@pytest.fixture
def event_detail_client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(events.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn_found() -> AsyncIterator[mock.AsyncMock]:
        m = mock.AsyncMock()
        m.fetchrow.return_value = _EVENT_ROW
        yield m

    fa.dependency_overrides[get_db_conn] = _conn_found
    with TestClient(fa) as c:
        yield c


@pytest.fixture
def event_missing_client() -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(events.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn_missing() -> AsyncIterator[mock.AsyncMock]:
        m = mock.AsyncMock()
        m.fetchrow.return_value = None
        yield m

    fa.dependency_overrides[get_db_conn] = _conn_missing
    with TestClient(fa) as c:
        yield c


class TestEventsApi:
    def test_create_event(self, client: TestClient) -> None:
        r = client.post(
            f"/api/v1/orgs/{_ORG}/events?project_id={_PROJ}",
            json={
                "event_type": "user_action",
                "payload": {"action": "click"},
            },
        )
        assert r.status_code == 201
        assert "id" in r.json()

    def test_list_events_empty(self, client: TestClient) -> None:
        r = client.get(f"/api/v1/orgs/{_ORG}/events")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_event_by_id_ok(self, event_detail_client: TestClient) -> None:
        r = event_detail_client.get(f"/api/v1/orgs/{_ORG}/events/{_EVENT_ID}")
        assert r.status_code == 200
        body = r.json()
        assert body["event_type"] == "service_down"
        assert body["trace_id"] == "trc_abc123"

    def test_get_event_by_id_not_found(self, event_missing_client: TestClient) -> None:
        r = event_missing_client.get(f"/api/v1/orgs/{_ORG}/events/{_EVENT_ID}")
        assert r.status_code == 404

    def test_list_events_offset_zero_is_default(self, client: TestClient) -> None:
        with mock.patch(
            "aegis.server.api.routers.events.recent_events",
            new_callable=mock.AsyncMock,
            return_value=[],
        ) as m:
            r = client.get(f"/api/v1/orgs/{_ORG}/events")
        assert r.status_code == 200
        _, kwargs = m.call_args
        assert kwargs.get("offset", 0) == 0

    def test_list_events_passes_offset_to_persistence(self, client: TestClient) -> None:
        with mock.patch(
            "aegis.server.api.routers.events.recent_events",
            new_callable=mock.AsyncMock,
            return_value=[],
        ) as m:
            r = client.get(f"/api/v1/orgs/{_ORG}/events?offset=50")
        assert r.status_code == 200
        _, kwargs = m.call_args
        assert kwargs["offset"] == 50

    def test_list_events_offset_negative_rejected(self, client: TestClient) -> None:
        r = client.get(f"/api/v1/orgs/{_ORG}/events?offset=-1")
        assert r.status_code == 422


class TestAlertsApi:
    def test_ingest_alert(self, client: TestClient) -> None:
        with (
            mock.patch(
                "aegis.server.api.routers.alerts.run_brain_pipeline",
                new_callable=mock.AsyncMock,
                return_value={"stage": "triage_only", "triage": {}},
            ),
            mock.patch(
                "aegis.server.services.incident_correlation.cluster_signal",
                new_callable=mock.AsyncMock,
                return_value=(uuid.uuid4(), True),
            ),
        ):
            r = client.post(
                f"/api/v1/orgs/{_ORG}/alerts/ingest?project_id={_PROJ}",
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
        assert "incident_id" in body and body["incident_is_new"] is True
