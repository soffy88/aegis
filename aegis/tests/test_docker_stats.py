"""Tests for GET /api/v1/docker/containers/{c}/stats (BATCH 19 §A)."""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from aegis.server.app import create_app

client = TestClient(create_app())


def _mock_stats_raw() -> dict:
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200_000_000, "percpu_usage": [100_000_000, 100_000_000]},
            "system_cpu_usage": 1_000_000_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 900_000_000,
        },
        "memory_stats": {"usage": 104_857_600, "limit": 8_589_934_592},
        "networks": {
            "eth0": {"rx_bytes": 1024_000, "tx_bytes": 512_000},
        },
    }


def test_stats_returns_cpu_mem_net() -> None:
    """Stats endpoint returns CPU%, Mem MB, Net I/O."""
    mock_container = MagicMock()
    mock_container.stats.return_value = _mock_stats_raw()

    mock_client = MagicMock()
    mock_client.containers.get.return_value = mock_container

    with mock.patch("docker.from_env", return_value=mock_client):
        resp = client.get("/api/v1/docker/containers/myapp/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["container"] == "myapp"
    assert data["cpu_pct"] == 200.0  # (100M/100M)*2*100
    assert data["mem_mb"] == 100.0
    assert data["net_rx_kb"] == 1000.0


def test_stats_container_not_found() -> None:
    """Stats for nonexistent container returns 502."""
    import docker.errors  # noqa: PLC0415

    mock_client = MagicMock()
    mock_client.containers.get.side_effect = docker.errors.NotFound("not found")

    with mock.patch("docker.from_env", return_value=mock_client):
        resp = client.get("/api/v1/docker/containers/ghost/stats")

    assert resp.status_code == 502
