"""Tests for project health protocol (BATCH 18 §B.4)."""

from __future__ import annotations

import httpx
import pytest
import respx
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

    disc_mod._discovery_cache = ([], 1e18)  # Cached empty, far-future timestamp


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    return TestClient(app)


class TestHealthParsing:
    """§B.4: health schema parsing compliance."""

    def test_valid_health_json_parsed(self, client: TestClient) -> None:
        """Standard ProjectHealth JSON is parsed correctly."""
        register_project("myapp", "http://myapp:8000/health")

        with respx.mock:
            respx.get("http://myapp:8000/health").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "version": "1.0.0",
                        "checks": {"db": {"status": "ok"}},
                        "timestamp": "2026-05-23T00:00:00Z",
                    },
                )
            )
            resp = client.get("/api/v1/projects/myapp/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["version"] == "1.0.0"
        assert data["checks"] == {"db": {"status": "ok"}}

    def test_old_format_http_200_inferred_ok(self, client: TestClient) -> None:
        """HTTP 200 with non-JSON body → status inferred as 'ok'."""
        register_project("legacy", "http://legacy:3000/health")

        with respx.mock:
            respx.get("http://legacy:3000/health").mock(return_value=httpx.Response(200, text="OK"))
            resp = client.get("/api/v1/projects/legacy/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_http_5xx_inferred_down(self, client: TestClient) -> None:
        """HTTP 500 → status inferred as 'down'."""
        register_project("broken", "http://broken:8080/health")

        with respx.mock:
            respx.get("http://broken:8080/health").mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            resp = client.get("/api/v1/projects/broken/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "down"

    def test_connection_timeout_inferred_down(self, client: TestClient) -> None:
        """Connection timeout → status = 'down'."""
        register_project("unreachable", "http://unreachable:9999/health")

        with respx.mock:
            respx.get("http://unreachable:9999/health").mock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            resp = client.get("/api/v1/projects/unreachable/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "down"


class TestProjectsList:
    """§B.4: list endpoint returns aggregated health."""

    def test_list_projects_empty(self, client: TestClient) -> None:
        """No registered projects → empty list."""
        resp = client.get("/api/v1/projects")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_projects_with_health(self, client: TestClient) -> None:
        """Registered project appears in list with status."""
        register_project("svc", "http://svc:8000/health")

        with respx.mock:
            respx.get("http://svc:8000/health").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status": "degraded",
                        "timestamp": "2026-05-23T01:00:00Z",
                    },
                )
            )
            resp = client.get("/api/v1/projects")

        assert resp.status_code == 200
        projects = resp.json()
        assert len(projects) == 1
        assert projects[0]["name"] == "svc"
        assert projects[0]["status"] == "degraded"


class TestProjectNotFound:
    def test_unknown_project_404(self, client: TestClient) -> None:
        """Requesting health for unregistered project → 404."""
        resp = client.get("/api/v1/projects/nonexistent/health")
        assert resp.status_code == 404
