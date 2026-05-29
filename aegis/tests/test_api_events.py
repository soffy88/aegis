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


class TestAlertsApi:
    def test_ingest_alert(self, client: TestClient) -> None:
        with mock.patch(
            "aegis.server.api.routers.alerts.run_brain_pipeline",
            new_callable=mock.AsyncMock,
            return_value={"stage": "triage_only", "triage": {}},
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
