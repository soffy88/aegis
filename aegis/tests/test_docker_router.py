"""Tests for docker router → oprim integration."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest import mock
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import docker as docker_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
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
    fa.include_router(docker_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> Generator[mock.AsyncMock, None, None]:
        yield mock.AsyncMock()

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


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


def test_inspect_uses_oprim(client: TestClient) -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_inspect",
        return_value=_mock_inspect_result(),
    ):
        resp = client.get(f"/api/v1/orgs/{_ORG}/docker/containers/abc123")
    assert resp.status_code == 200
    assert resp.json()["container_id"] == "abc123"


def test_start_uses_oprim(client: TestClient) -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_start",
        return_value=_mock_op_result(),
    ):
        resp = client.post(f"/api/v1/orgs/{_ORG}/docker/containers/abc123/start")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_stop_uses_oprim(client: TestClient) -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_stop",
        return_value=_mock_op_result(),
    ):
        resp = client.post(f"/api/v1/orgs/{_ORG}/docker/containers/abc123/stop")
    assert resp.status_code == 200


def test_restart_uses_oprim(client: TestClient) -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_restart",
        return_value=_mock_op_result(),
    ):
        resp = client.post(f"/api/v1/orgs/{_ORG}/docker/containers/abc123/restart")
    assert resp.status_code == 200


def test_logs_uses_oprim(client: TestClient) -> None:
    log_line = MagicMock()
    log_line.model_dump.return_value = {"timestamp": "2026-05-24T00:00:00Z", "message": "started"}

    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_logs",
        return_value=[log_line],
    ):
        resp = client.get(f"/api/v1/orgs/{_ORG}/docker/containers/abc123/logs")
    assert resp.status_code == 200
    assert resp.json()["lines"][0]["message"] == "started"


def test_inspect_oprim_error_returns_502(client: TestClient) -> None:
    from oprim._exceptions import OprimError

    with mock.patch(
        "aegis.server.api.routers.docker.docker_container_inspect",
        side_effect=OprimError("container not found"),
    ):
        resp = client.get(f"/api/v1/orgs/{_ORG}/docker/containers/ghost")
    assert resp.status_code == 502


def test_list_containers_uses_oprim(client: TestClient) -> None:
    c1 = MagicMock()
    c1.model_dump.return_value = {"container_id": "c1", "state": "running"}
    with mock.patch(
        "aegis.server.api.routers.docker.docker_ps",
        return_value=[c1],
    ):
        resp = client.get(f"/api/v1/orgs/{_ORG}/docker/containers")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["container_id"] == "c1"


def test_list_containers_routes_to_node_docker_host() -> None:
    """With ?node_id=, oprim must target that node's docker_host_url (#9)."""
    node_id = uuid.uuid4()
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"docker_host_url": "tcp://10.0.0.9:2375"}

    fa = FastAPI()
    fa.include_router(docker_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> Generator[mock.AsyncMock, None, None]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _conn
    c = TestClient(fa, raise_server_exceptions=False)

    ps = mock.MagicMock(return_value=[])
    with mock.patch("aegis.server.api.routers.docker.docker_ps", ps):
        resp = c.get(f"/api/v1/orgs/{_ORG}/docker/containers?node_id={node_id}")
    assert resp.status_code == 200
    assert ps.call_args.kwargs["docker_host"] == "tcp://10.0.0.9:2375"


def test_list_containers_unknown_node_404() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = None  # node not found

    fa = FastAPI()
    fa.include_router(docker_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> Generator[mock.AsyncMock, None, None]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _conn
    c = TestClient(fa, raise_server_exceptions=False)
    resp = c.get(f"/api/v1/orgs/{_ORG}/docker/containers?node_id={uuid.uuid4()}")
    assert resp.status_code == 404


def test_list_containers_empty(client: TestClient) -> None:
    with mock.patch(
        "aegis.server.api.routers.docker.docker_ps",
        return_value=[],
    ):
        resp = client.get(f"/api/v1/orgs/{_ORG}/docker/containers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_containers_rbac_unauthorized(client: TestClient) -> None:
    """Test that a user with no membership in the org gets 403."""

    async def _no_org_user() -> UserContext:
        return UserContext(
            user_id=_USER,
            email="bad@example.com",
            orgs=[],
        )

    client.app.dependency_overrides[get_current_user] = _no_org_user
    try:
        resp = client.get(f"/api/v1/orgs/{_ORG}/docker/containers")
        assert resp.status_code == 403
    finally:
        client.app.dependency_overrides[get_current_user] = _fake_user


def test_list_containers_oprim_error_returns_502(client: TestClient) -> None:
    from oprim._exceptions import OprimError

    with mock.patch(
        "aegis.server.api.routers.docker.docker_ps",
        side_effect=OprimError("docker daemon not responding"),
    ):
        resp = client.get(f"/api/v1/orgs/{_ORG}/docker/containers")
    assert resp.status_code == 502
    assert "docker daemon not responding" in resp.json()["detail"]


# ── privilege re-tiering: exec/terminal are admin+, host-shell is owner-only ────


def _user_with_role(role: str):
    async def _u() -> UserContext:
        return UserContext(
            user_id=_USER,
            email=f"{role}@example.com",
            orgs=[OrgInToken(org_id=_ORG, slug="test-org", role=role)],
        )

    return _u


def test_exec_rejected_for_operator(client: TestClient) -> None:
    """Arbitrary in-container exec must require admin+, not operator (host-RCE surface)."""
    client.app.dependency_overrides[get_current_user] = _user_with_role("operator")
    try:
        resp = client.post(
            f"/api/v1/orgs/{_ORG}/docker/containers/abc123/exec",
            json={"command": ["cat", "/etc/passwd"]},
        )
        assert resp.status_code == 403
    finally:
        client.app.dependency_overrides[get_current_user] = _fake_user


def test_exec_allowed_for_admin(client: TestClient) -> None:
    client.app.dependency_overrides[get_current_user] = _user_with_role("admin")
    result = MagicMock()
    result.model_dump.return_value = {"exit_code": 0, "output": "root"}
    try:
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_exec",
            return_value=result,
        ):
            resp = client.post(
                f"/api/v1/orgs/{_ORG}/docker/containers/abc123/exec",
                json={"command": ["id"]},
            )
        assert resp.status_code == 200
    finally:
        client.app.dependency_overrides[get_current_user] = _fake_user


def test_host_shell_rejected_for_admin(client: TestClient) -> None:
    """host-shell mints a --privileged -v /:/host container → owner-only break-glass."""
    client.app.dependency_overrides[get_current_user] = _user_with_role("admin")
    try:
        resp = client.post(f"/api/v1/orgs/{_ORG}/docker/host-shell")
        assert resp.status_code == 403
    finally:
        client.app.dependency_overrides[get_current_user] = _fake_user


def test_host_shell_allowed_for_owner(client: TestClient) -> None:
    already_running = MagicMock(stdout="containerid\n")
    with mock.patch("subprocess.run", return_value=already_running):
        resp = client.post(f"/api/v1/orgs/{_ORG}/docker/host-shell")  # _fake_user is owner
    assert resp.status_code == 200
    assert resp.json()["container"] == "aegis-host-shell"
