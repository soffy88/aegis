"""Tests for postmortem generation endpoint + service."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import incidents as incidents_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_INCIDENT = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _user(role: str = "admin") -> UserContext:
    return UserContext(
        user_id=_USER,
        email="admin@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role=role)],
    )


def _incident_row(*, postmortem_md: str | None = None) -> dict:
    return {
        "id": _INCIDENT,
        "org_id": _ORG,
        "title": "DB connection pool exhausted",
        "started_at": datetime.now(UTC),
        "resolved_at": None,
        "severity": "critical",
        "status": "open",
        "postmortem_md": postmortem_md,
        "created_at": datetime.now(UTC),
    }


def _make_app(role: str, conn: mock.AsyncMock) -> FastAPI:
    fa = FastAPI()
    fa.include_router(incidents_router.router)
    u = _user(role)
    fa.dependency_overrides[get_current_user] = lambda: u

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _conn
    return fa


class TestListIncidents:
    def test_list_returns_incidents(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = [_incident_row()]
        with TestClient(_make_app("member", conn), raise_server_exceptions=False) as c:
            r = c.get(f"/api/v1/orgs/{_ORG}/incidents")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["title"] == "DB connection pool exhausted"

    def test_viewer_can_list_incidents(self) -> None:
        conn = mock.AsyncMock()
        conn.fetch.return_value = []
        with TestClient(_make_app("viewer", conn), raise_server_exceptions=False) as c:
            r = c.get(f"/api/v1/orgs/{_ORG}/incidents")
        assert r.status_code == 200


class TestGetIncident:
    def test_get_returns_incident_detail(self) -> None:
        conn = mock.AsyncMock()
        conn.fetchrow.return_value = _incident_row()
        conn.fetch.return_value = []  # events
        with TestClient(_make_app("member", conn), raise_server_exceptions=False) as c:
            r = c.get(f"/api/v1/orgs/{_ORG}/incidents/{_INCIDENT}")
        assert r.status_code == 200
        assert r.json()["id"] == str(_INCIDENT)

    def test_get_unknown_incident_returns_404(self) -> None:
        conn = mock.AsyncMock()
        conn.fetchrow.return_value = None
        with TestClient(_make_app("member", conn), raise_server_exceptions=False) as c:
            r = c.get(f"/api/v1/orgs/{_ORG}/incidents/{uuid.uuid4()}")
        assert r.status_code == 404


class TestGeneratePostmortem:
    def test_operator_can_generate_postmortem(self) -> None:
        conn = mock.AsyncMock()
        conn.fetchrow.side_effect = [
            _incident_row(),  # fetch incident
            {"id": _INCIDENT, "postmortem_md": "# Postmortem\n\nRoot cause: ..."},  # after update
        ]
        conn.fetch.return_value = []  # events

        with mock.patch("aegis.server.api.routers.incidents.run_postmortem") as mock_run:
            mock_run.return_value = "# Postmortem\n\nRoot cause: ..."
            with TestClient(_make_app("operator", conn), raise_server_exceptions=False) as c:
                r = c.post(f"/api/v1/orgs/{_ORG}/incidents/{_INCIDENT}/postmortem")

        assert r.status_code == 200
        assert "postmortem_md" in r.json()

    def test_viewer_cannot_generate_postmortem(self) -> None:
        conn = mock.AsyncMock()
        conn.fetchrow.return_value = _incident_row()
        with TestClient(_make_app("viewer", conn), raise_server_exceptions=False) as c:
            r = c.post(f"/api/v1/orgs/{_ORG}/incidents/{_INCIDENT}/postmortem")
        assert r.status_code == 403

    def test_generate_postmortem_unknown_incident_returns_404(self) -> None:
        conn = mock.AsyncMock()
        conn.fetchrow.return_value = None
        with TestClient(_make_app("operator", conn), raise_server_exceptions=False) as c:
            r = c.post(f"/api/v1/orgs/{_ORG}/incidents/{uuid.uuid4()}/postmortem")
        assert r.status_code == 404

    def test_generate_postmortem_stores_result(self) -> None:
        conn = mock.AsyncMock()
        conn.fetchrow.side_effect = [
            _incident_row(),
            {"id": _INCIDENT, "postmortem_md": "# PM"},
        ]
        conn.fetch.return_value = []
        conn.execute.return_value = "UPDATE 1"

        with mock.patch("aegis.server.api.routers.incidents.run_postmortem") as mock_run:
            mock_run.return_value = "# PM"
            with TestClient(_make_app("operator", conn), raise_server_exceptions=False) as c:
                c.post(f"/api/v1/orgs/{_ORG}/incidents/{_INCIDENT}/postmortem")

        # execute should have been called to store the postmortem_md
        conn.execute.assert_called()


class TestRunPostmortemService:
    """Unit tests for the postmortem service function itself."""

    @pytest.mark.asyncio
    async def test_run_postmortem_calls_omodul(self) -> None:
        from aegis.server.orchestration.postmortem import run_postmortem

        incident = _incident_row()
        events: list[dict] = []

        with mock.patch(
            "aegis.server.orchestration.postmortem._call_omodul_postmortem"
        ) as mock_call:
            mock_call.return_value = "# Postmortem\n\nTimeline: ..."
            result = await run_postmortem(incident=incident, events=events)

        assert "Postmortem" in result
        mock_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_postmortem_returns_markdown_string(self) -> None:
        from aegis.server.orchestration.postmortem import run_postmortem

        incident = _incident_row()
        events: list[dict] = []

        with mock.patch(
            "aegis.server.orchestration.postmortem._call_omodul_postmortem"
        ) as mock_call:
            mock_call.return_value = "# Postmortem\n\nResolution: restart service"
            result = await run_postmortem(incident=incident, events=events)

        assert isinstance(result, str)
        assert result.startswith("# ")
