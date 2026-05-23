"""Tests for project discovery via Docker labels (BATCH 18 §C.5)."""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

import pytest

from aegis.server.services.project_discovery import (
    discover_projects,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Clear discovery cache between tests."""
    import aegis.server.services.project_discovery as mod

    mod._discovery_cache = None


def _make_container(
    name: str,
    project: str,
    health_path: str = "/health",
    health_port: str | None = None,
    role: str | None = None,
    host_port: str = "8000",
) -> MagicMock:
    """Create a mock Docker container with aegis labels."""
    c = MagicMock()
    c.short_id = name[:12]
    c.name = name
    c.status = "running"
    c.image.tags = [f"{name}:latest"]
    labels = {"aegis.project": project, "aegis.health.path": health_path}
    if health_port:
        labels["aegis.health.port"] = health_port
    if role:
        labels["aegis.role"] = role
    c.labels = labels
    c.attrs = {"NetworkSettings": {"Ports": {"8000/tcp": [{"HostPort": host_port}]}}}
    return c


class TestProjectDiscovery:
    def test_multi_container_same_project(self) -> None:
        """Multiple containers with same aegis.project are grouped."""
        containers = [
            _make_container("web-1", "stratum", host_port="8000"),
            _make_container("worker-1", "stratum", host_port="8001", role="worker"),
        ]

        mock_client = MagicMock()
        mock_client.containers.list.return_value = containers

        with mock.patch("docker.from_env", return_value=mock_client):
            projects = discover_projects()

        assert len(projects) == 1
        assert projects[0].name == "stratum"
        assert len(projects[0].containers) == 2

    def test_missing_label_not_discovered(self) -> None:
        """Containers without aegis.project label are not discovered."""
        c = MagicMock()
        c.short_id = "abc123"
        c.name = "random-container"
        c.status = "running"
        c.image.tags = ["nginx:latest"]
        c.labels = {}  # No aegis.project label
        c.attrs = {"NetworkSettings": {"Ports": {}}}

        # Docker filters by label, so this container shouldn't appear
        # But test the edge case where label value is empty
        c2 = MagicMock()
        c2.short_id = "def456"
        c2.name = "empty-label"
        c2.status = "running"
        c2.image.tags = ["app:latest"]
        c2.labels = {"aegis.project": ""}
        c2.attrs = {"NetworkSettings": {"Ports": {}}}

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [c2]

        with mock.patch("docker.from_env", return_value=mock_client):
            projects = discover_projects()

        assert len(projects) == 0

    def test_health_path_default(self) -> None:
        """Default health path is /health when not specified."""
        c = _make_container("myapp", "tide")
        # Remove explicit health path to test default
        c.labels = {"aegis.project": "tide"}

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [c]

        with mock.patch("docker.from_env", return_value=mock_client):
            projects = discover_projects()

        assert len(projects) == 1
        assert projects[0].containers[0].health_path == "/health"

    def test_docker_unavailable_graceful_degradation(self) -> None:
        """When Docker is unavailable, returns empty list (no crash)."""
        with mock.patch("docker.from_env", side_effect=Exception("Cannot connect to Docker")):
            projects = discover_projects()

        assert projects == []

    def test_custom_health_port(self) -> None:
        """aegis.health.port label overrides auto-detection."""
        c = _make_container("myapp", "hevi", health_port="9090")

        mock_client = MagicMock()
        mock_client.containers.list.return_value = [c]

        with mock.patch("docker.from_env", return_value=mock_client):
            projects = discover_projects()

        assert projects[0].containers[0].health_port == 9090
        assert projects[0].health_url == "http://localhost:9090/health"
