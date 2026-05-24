"""Tests for docker router → oprim integration (C0c-2)."""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from aegis.server.app import create_app

client = TestClient(create_app())


def _mock_op_result() -> MagicMock:
    r = MagicMock()
    r.model_dump.return_value = {
        "container_id": "abc123",
        "operation": "start",
        "success": True,
        "elapsed_ms": 50,
        "state_before": "exited",
        "state_after": "running",
    }
    return r


def _mock_inspect_result() -> MagicMock:
    r = MagicMock()
    r.model_dump.return_value = {
        "container_id": "abc123",
        "name": "myapp",
        "image": "myapp:latest",
        "state": "running",
        "status": "Up 2 hours",
        "labels": {},
        "ports": [],
    }
    return r


def test_inspect_uses_oprim() -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_inspect",
        return_value=_mock_inspect_result(),
    ):
        resp = client.get("/api/v1/docker/containers/abc123")
    assert resp.status_code == 200
    assert resp.json()["container_id"] == "abc123"


def test_start_uses_oprim() -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_start",
        return_value=_mock_op_result(),
    ):
        resp = client.post("/api/v1/docker/containers/abc123/start")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_stop_uses_oprim() -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_stop",
        return_value=_mock_op_result(),
    ):
        resp = client.post("/api/v1/docker/containers/abc123/stop")
    assert resp.status_code == 200


def test_restart_uses_oprim() -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_restart",
        return_value=_mock_op_result(),
    ):
        resp = client.post("/api/v1/docker/containers/abc123/restart")
    assert resp.status_code == 200


def test_logs_uses_oprim() -> None:
    log_line = MagicMock()
    log_line.model_dump.return_value = {"timestamp": "2026-05-24T00:00:00Z", "message": "started"}

    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_logs",
        return_value=[log_line],
    ):
        resp = client.get("/api/v1/docker/containers/abc123/logs")
    assert resp.status_code == 200
    assert resp.json()["lines"][0]["message"] == "started"


def test_inspect_oprim_error_returns_502() -> None:
    from oprim._exceptions import OprimError

    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_inspect",
        side_effect=OprimError("container not found"),
    ):
        resp = client.get("/api/v1/docker/containers/ghost")
    assert resp.status_code == 502
