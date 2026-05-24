"""Tests for GET /api/v1/docker/containers/{c}/stats (走 oprim)."""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from aegis.server.app import create_app

client = TestClient(create_app())


def _mock_stats() -> MagicMock:
    s = MagicMock()
    s.model_dump.return_value = {
        "container_id": "myapp",
        "cpu_percent": 25.5,
        "memory_usage_bytes": 104_857_600,
        "memory_limit_bytes": 8_589_934_592,
        "memory_percent": 1.2,
        "network_rx_bytes": 1_024_000,
        "network_tx_bytes": 512_000,
        "block_read_bytes": 0,
        "block_write_bytes": 0,
        "pids": 5,
        "timestamp": "2026-05-24T00:00:00Z",
    }
    return s


def test_stats_returns_cpu_mem_net() -> None:
    """Stats endpoint returns CPU%, Mem MB, Net I/O via oprim."""
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_stats",
        return_value=_mock_stats(),
    ):
        resp = client.get("/api/v1/docker/containers/myapp/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["container"] == "myapp"
    assert data["cpu_pct"] == 25.5
    assert data["mem_mb"] == 100.0
    assert data["net_rx_kb"] == 1000.0


def test_stats_container_not_found() -> None:
    """Stats for nonexistent container returns 502."""
    from oprim._exceptions import OprimError

    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_stats",
        side_effect=OprimError("not found"),
    ):
        resp = client.get("/api/v1/docker/containers/ghost/stats")

    assert resp.status_code == 502
