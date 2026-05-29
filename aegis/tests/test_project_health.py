"""Tests for DB-backed project health probe endpoint (C1-4 migration)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from oprim.url_safety_check import URLSafetyResult

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import projects as projects_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ORG = uuid.UUID("99999999-9999-9999-9999-999999999999")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

_HEALTH_URL = "/api/v1/orgs/{org_id}/projects/{project_id}/health"

_SAFE_RESULT = URLSafetyResult(
    is_safe=True,
    reason=None,
    resolved_ips=["8.8.8.8"],
    failed_check=None,
)


def _project_row(
    *, org_id: uuid.UUID = _ORG, health_url: str | None = "http://svc:8000/health"
) -> dict:
    config = {"health_url": health_url} if health_url else None
    return {
        "id": _PROJ,
        "org_id": org_id,
        "slug": "test-proj",
        "name": "Test Project",
        "display_name": "Test Project",
        "environment": "prod",
        "docker_labels": None,
        "config": config,
        "archived_at": None,
        "created_at": datetime(2026, 1, 1),
    }


def _make_client(conn: mock.AsyncMock, role: str = "viewer") -> TestClient:
    fa = FastAPI()
    fa.include_router(projects_router.router)

    user = UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role=role)],
    )

    async def _fake_user() -> UserContext:
        return user

    async def _fake_conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_current_user] = _fake_user
    fa.dependency_overrides[get_db_conn] = _fake_conn
    return TestClient(fa, raise_server_exceptions=False)


def _probe(healthy: bool = True) -> mock.MagicMock:
    r = mock.MagicMock()
    r.healthy = healthy
    r.status_code = 200 if healthy else 500
    r.elapsed_ms = 12
    r.error = None if healthy else "connection refused"
    return r


class TestProjectHealthEndpoint:
    @pytest.fixture
    def conn(self) -> mock.AsyncMock:
        m = mock.AsyncMock()
        m.fetchrow.return_value = _project_row()
        return m

    def test_health_check_healthy_project(self, conn: mock.AsyncMock) -> None:
        """Healthy probe → healthy=True, status 200."""
        client = _make_client(conn)
        with (
            mock.patch(
                "aegis.server.api.routers.projects.url_safety_check",
                return_value=_SAFE_RESULT,
            ),
            mock.patch(
                "aegis.server.api.routers.projects.http_health_probe", return_value=_probe(True)
            ),
        ):
            r = client.get(_HEALTH_URL.format(org_id=_ORG, project_id=_PROJ))
        assert r.status_code == 200
        body = r.json()
        assert body["healthy"] is True
        assert body["slug"] == "test-proj"
        assert body["health_url"] == "http://svc:8000/health"
        assert body["project_id"] == str(_PROJ)

    def test_health_check_unhealthy_project(self, conn: mock.AsyncMock) -> None:
        """Unhealthy probe → healthy=False, still status 200 (probe result, not HTTP error)."""
        client = _make_client(conn)
        with (
            mock.patch(
                "aegis.server.api.routers.projects.url_safety_check",
                return_value=_SAFE_RESULT,
            ),
            mock.patch(
                "aegis.server.api.routers.projects.http_health_probe", return_value=_probe(False)
            ),
        ):
            r = client.get(_HEALTH_URL.format(org_id=_ORG, project_id=_PROJ))
        assert r.status_code == 200
        body = r.json()
        assert body["healthy"] is False
        assert body["error"] == "connection refused"
        assert body["status_code"] == 500

    def test_health_check_no_url_configured_400(self, conn: mock.AsyncMock) -> None:
        """project.config has no health_url → 400."""
        conn.fetchrow.return_value = _project_row(health_url=None)
        client = _make_client(conn)
        r = client.get(_HEALTH_URL.format(org_id=_ORG, project_id=_PROJ))
        assert r.status_code == 400
        assert "health_url" in r.json()["detail"]

    def test_health_check_project_not_found_404(self, conn: mock.AsyncMock) -> None:
        """Unknown project_id → 404."""
        conn.fetchrow.return_value = None
        client = _make_client(conn)
        r = client.get(_HEALTH_URL.format(org_id=_ORG, project_id=_PROJ))
        assert r.status_code == 404

    def test_health_check_wrong_org_404(self, conn: mock.AsyncMock) -> None:
        """Project exists but belongs to a different org → 404 (org isolation)."""
        conn.fetchrow.return_value = _project_row(org_id=_OTHER_ORG)
        client = _make_client(conn)
        r = client.get(_HEALTH_URL.format(org_id=_ORG, project_id=_PROJ))
        assert r.status_code == 404


class TestHealthUrlSsrfValidation:
    """_validate_health_url blocks SSRF-risky URLs before the probe is made."""

    @pytest.fixture
    def conn(self) -> mock.AsyncMock:
        m = mock.AsyncMock()
        m.fetchrow.return_value = _project_row()
        return m

    def test_private_ip_url_rejected_400(self, conn: mock.AsyncMock) -> None:
        """health_url resolving to RFC1918 address → 400, probe never called."""
        conn.fetchrow.return_value = _project_row(health_url="http://internal:8000/health")
        unsafe_result = URLSafetyResult(
            is_safe=False,
            reason="is_private_blocked",
            resolved_ips=["192.168.1.50"],
            failed_check="is_private",
        )
        client = _make_client(conn)
        with (
            mock.patch(
                "aegis.server.api.routers.projects.url_safety_check",
                return_value=unsafe_result,
            ),
            mock.patch("aegis.server.api.routers.projects.http_health_probe") as mock_probe,
        ):
            r = client.get(_HEALTH_URL.format(org_id=_ORG, project_id=_PROJ))
        assert r.status_code == 400
        assert "private" in r.json()["detail"] or "reserved" in r.json()["detail"]
        mock_probe.assert_not_called()

    def test_bad_scheme_url_rejected_400(self, conn: mock.AsyncMock) -> None:
        """health_url with file:// scheme → 400 without DNS resolution."""
        conn.fetchrow.return_value = _project_row(health_url="file:///etc/passwd")
        scheme_result = URLSafetyResult(
            is_safe=False,
            reason="scheme_not_allowed",
            resolved_ips=[],
            failed_check=None,
        )
        client = _make_client(conn)
        with (
            mock.patch(
                "aegis.server.api.routers.projects.url_safety_check",
                return_value=scheme_result,
            ),
            mock.patch("aegis.server.api.routers.projects.http_health_probe") as mock_probe,
        ):
            r = client.get(_HEALTH_URL.format(org_id=_ORG, project_id=_PROJ))
        assert r.status_code == 400
        assert "scheme" in r.json()["detail"]
        mock_probe.assert_not_called()

    def test_redirect_to_private_ip_rejected(self, conn: mock.AsyncMock) -> None:
        """http_health_probe is called with follow_redirects=False.

        Simulates: target URL returns 302 → 169.254.169.254 (cloud metadata).
        Because follow_redirects=False, oprim returns the 302 response as-is
        (healthy=False, status_code=302). The metadata host is never contacted
        and its content never appears in the response.
        """
        # probe returns the 302 result without following it
        redirect_result = mock.MagicMock()
        redirect_result.healthy = False
        redirect_result.status_code = 302
        redirect_result.elapsed_ms = 5
        redirect_result.error = "redirect not followed"

        client = _make_client(conn)
        with (
            mock.patch(
                "aegis.server.api.routers.projects.url_safety_check",
                return_value=_SAFE_RESULT,
            ),
            mock.patch(
                "aegis.server.api.routers.projects.http_health_probe",
                return_value=redirect_result,
            ) as mock_probe,
        ):
            r = client.get(_HEALTH_URL.format(org_id=_ORG, project_id=_PROJ))

        assert r.status_code == 200
        body = r.json()
        # probe was not followed to the private redirect target
        assert body["status_code"] == 302
        assert "169.254.169.254" not in str(body)
        # verify follow_redirects=False was explicitly passed
        mock_probe.assert_called_once_with(
            url="http://svc:8000/health", timeout_sec=5, follow_redirects=False
        )
