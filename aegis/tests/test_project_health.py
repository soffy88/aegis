"""Tests for project health protocol (走 oprim.http_health_probe)."""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from aegis.server.api.routers.projects import (
    _HEALTH_CACHE,
    _PROJECTS,
    register_project,
)
from aegis.server.app import create_app


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Reset module-level state between tests."""
    _PROJECTS.clear()
    _HEALTH_CACHE.clear()


@pytest.fixture(autouse=True)
def _mock_discovery() -> None:
    """Disable Docker discovery in unit tests."""
    import aegis.server.services.project_discovery as disc_mod

    disc_mod._discovery_cache = ([], 1e18)


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


def _probe_result(healthy: bool = True) -> MagicMock:
    r = MagicMock()
    r.healthy = healthy
    r.status_code = 200 if healthy else 500
    r.elapsed_ms = 10
    r.error = None if healthy else "connection refused"
    return r


class TestHealthParsing:
    """Health endpoint uses oprim.http_health_probe."""

    def test_healthy_project(self, client: TestClient) -> None:
        """Healthy probe → status ok."""
        register_project("myapp", "http://myapp:8000/health")

        with mock.patch(
            "aegis.server.api.routers.projects.http_health_probe",
            return_value=_probe_result(healthy=True),
        ):
            resp = client.get("/api/v1/projects/myapp/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_unhealthy_project(self, client: TestClient) -> None:
        """Unhealthy probe → status down."""
        register_project("broken", "http://broken:8080/health")

        with mock.patch(
            "aegis.server.api.routers.projects.http_health_probe",
            return_value=_probe_result(healthy=False),
        ):
            resp = client.get("/api/v1/projects/broken/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "down"

    def test_probe_exception_inferred_down(self, client: TestClient) -> None:
        """Probe returning unhealthy → status down."""
        register_project("unreachable", "http://unreachable:9999/health")

        with mock.patch(
            "aegis.server.api.routers.projects.http_health_probe",
            return_value=_probe_result(healthy=False),
        ):
            resp = client.get("/api/v1/projects/unreachable/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "down"


class TestProjectsList:
    def test_list_projects_empty(self, client: TestClient) -> None:
        """No registered projects → empty list."""
        resp = client.get("/api/v1/projects")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_projects_with_health(self, client: TestClient) -> None:
        """Registered project appears in list with status."""
        register_project("svc", "http://svc:8000/health")

        with mock.patch(
            "aegis.server.api.routers.projects.http_health_probe",
            return_value=_probe_result(healthy=True),
        ):
            resp = client.get("/api/v1/projects")

        assert resp.status_code == 200
        projects = resp.json()
        assert len(projects) == 1
        assert projects[0]["name"] == "svc"
        assert projects[0]["status"] == "ok"


class TestProjectNotFound:
    def test_unknown_project_404(self, client: TestClient) -> None:
        """Requesting health for unregistered project → 404."""
        resp = client.get("/api/v1/projects/nonexistent/health")
        assert resp.status_code == 404
