"""Tests for project discovery via Docker labels (走 oprim.docker_container_list)."""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

import pytest

from aegis.server.services.project_discovery import discover_projects


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """Clear discovery cache between tests."""
    import aegis.server.services.project_discovery as mod

    mod._discovery_cache = None


def _make_container_info(
    name: str,
    project: str,
    health_path: str = "/health",
    health_port: str | None = None,
    role: str | None = None,
    host_port: str = "8000",
) -> MagicMock:
    """Create a mock oprim ContainerInfo."""
    info = MagicMock()
    info.container_id = name + "abcdef123456"
    info.name = name
    info.image = f"{name}:latest"
    info.status = "running"
    labels = {"aegis.project": project, "aegis.health.path": health_path}
    if health_port:
        labels["aegis.health.port"] = health_port
    if role:
        labels["aegis.role"] = role
    info.labels = labels
    info.ports = [{"HostPort": host_port}]
    return info


class TestProjectDiscovery:
    def test_multi_container_same_project(self) -> None:
        """Multiple containers with same aegis.project are grouped."""
        containers = [
            _make_container_info("web-1", "stratum", host_port="8000"),
            _make_container_info("worker-1", "stratum", host_port="8001", role="worker"),
        ]

        with mock.patch(
            "aegis.server.services.project_discovery.docker_container_list",
            return_value=containers,
        ):
            projects = discover_projects()

        assert len(projects) == 1
        assert projects[0].name == "stratum"
        assert len(projects[0].containers) == 2

    def test_missing_label_not_discovered(self) -> None:
        """Containers with empty aegis.project label are not discovered."""
        info = MagicMock()
        info.container_id = "def456abcdef"
        info.name = "empty-label"
        info.image = "app:latest"
        info.status = "running"
        info.labels = {"aegis.project": ""}
        info.ports = []

        with mock.patch(
            "aegis.server.services.project_discovery.docker_container_list",
            return_value=[info],
        ):
            projects = discover_projects()

        assert len(projects) == 0

    def test_health_path_default(self) -> None:
        """Default health path is /health when not specified."""
        info = _make_container_info("myapp", "tide")
        info.labels = {"aegis.project": "tide"}

        with mock.patch(
            "aegis.server.services.project_discovery.docker_container_list",
            return_value=[info],
        ):
            projects = discover_projects()

        assert len(projects) == 1
        assert projects[0].containers[0].health_path == "/health"

    def test_docker_unavailable_graceful_degradation(self) -> None:
        """When Docker is unavailable, returns empty list (no crash)."""
        with mock.patch(
            "aegis.server.services.project_discovery.docker_container_list",
            side_effect=Exception("Cannot connect to Docker"),
        ):
            projects = discover_projects()

        assert projects == []

    def test_custom_health_port(self) -> None:
        """aegis.health.port label overrides auto-detection."""
        info = _make_container_info("myapp", "hevi", health_port="9090")

        with mock.patch(
            "aegis.server.services.project_discovery.docker_container_list",
            return_value=[info],
        ):
            projects = discover_projects()

        assert projects[0].containers[0].health_port == 9090
        assert projects[0].health_url == "http://localhost:9090/health"
