"""Tests for docker, apps, and domains routers (C1-4 org-scoped paths)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import apps as apps_router
from aegis.server.api.routers import docker as docker_router
from aegis.server.api.routers import domains
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_APP_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_USER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


# ---------------------------------------------------------------------------
# Shared auth mock
# ---------------------------------------------------------------------------


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="owner")],
    )


def _project_row() -> dict:
    """A valid asyncpg row dict for a Project."""
    return {
        "id": _PROJ,
        "org_id": _ORG,
        "slug": "test-proj",
        "name": "Test Project",
        "display_name": "Test Project",
        "environment": "prod",
        "docker_labels": None,
        "config": None,
        "archived_at": None,
        "created_at": datetime(2026, 1, 1),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app(*routers: object) -> FastAPI:
    fa = FastAPI()
    for r in routers:
        fa.include_router(r)  # type: ignore[arg-type]
    return fa


@pytest.fixture
def docker_client() -> Generator[TestClient, None, None]:
    fa = _make_app(docker_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield mock.AsyncMock()

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def apps_conn() -> mock.AsyncMock:
    m = mock.AsyncMock()
    # list_apps
    m.fetch.return_value = []
    # get_app — default not found
    m.fetchrow.return_value = None
    # install — returns id
    m.fetchval.return_value = _APP_ID
    # delete
    m.execute.return_value = "DELETE 1"
    return m


@pytest.fixture
def apps_client(apps_conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
    fa = _make_app(apps_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield apps_conn

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def domains_conn() -> mock.AsyncMock:
    m = mock.AsyncMock()
    m.fetch.return_value = []
    m.execute.return_value = "DELETE 1"
    # register_domain looks up project first
    m.fetchrow.return_value = _project_row()
    return m


@pytest.fixture
def domains_client(domains_conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
    fa = _make_app(domains.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield domains_conn

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Docker router tests
# ---------------------------------------------------------------------------


_INSPECT_DATA: dict[str, object] = {
    "container_id": "abc123",
    "container_name": "nginx",
    "state": "running",
    "status": "Up 2 hours",
    "image": "nginx:latest",
    "started_at": None,
    "finished_at": None,
    "restart_count": 0,
    "exit_code": None,
    "health": None,
    "ports": {},
    "mounts": [],
    "env": [],
    "labels": {},
}


_LIST_ITEM: dict[str, object] = {
    "container_id": "abc123",
    "name": "nginx",
    "image": "nginx:latest",
    "state": "running",
    "status": "Up 2 hours",
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "health": None,
    "restart_count": 0,
    "labels": {},
    "ports": {},
    "mounts": [],
}


class TestDockerRouter:
    def test_list_containers_returns_list(self, docker_client: TestClient) -> None:
        item = mock.MagicMock()
        item.model_dump.return_value = _LIST_ITEM
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_list",
            return_value=[item],
        ):
            r = docker_client.get(f"/api/v1/orgs/{_ORG}/docker/containers")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert r.json()[0]["name"] == "nginx"

    def test_list_containers_all_flag_forwarded(self, docker_client: TestClient) -> None:
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_list",
            return_value=[],
        ) as mock_list:
            docker_client.get(f"/api/v1/orgs/{_ORG}/docker/containers?all=true")
        mock_list.assert_called_once_with(all=True)

    def test_list_containers_oprim_error_502(self, docker_client: TestClient) -> None:
        from oprim._exceptions import OprimError

        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_list",
            side_effect=OprimError("daemon down"),
        ):
            r = docker_client.get(f"/api/v1/orgs/{_ORG}/docker/containers")
        assert r.status_code == 502

    def test_inspect_ok(self, docker_client: TestClient) -> None:
        result = mock.MagicMock()
        result.model_dump.return_value = _INSPECT_DATA
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_inspect",
            return_value=result,
        ):
            r = docker_client.get(f"/api/v1/orgs/{_ORG}/docker/containers/nginx")
        assert r.status_code == 200
        assert r.json()["container_id"] == "abc123"

    def test_inspect_oprim_error_502(self, docker_client: TestClient) -> None:
        from oprim._exceptions import OprimError

        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_inspect",
            side_effect=OprimError("daemon down"),
        ):
            r = docker_client.get(f"/api/v1/orgs/{_ORG}/docker/containers/missing")
        assert r.status_code == 502
        assert "daemon down" in r.json()["detail"]

    def test_start_ok(self, docker_client: TestClient) -> None:
        result = mock.MagicMock()
        result.model_dump.return_value = {
            "container_id": "abc123",
            "operation": "start",
            "success": True,
            "elapsed_ms": 50,
            "state_before": "exited",
            "state_after": "running",
        }
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_start",
            return_value=result,
        ):
            r = docker_client.post(f"/api/v1/orgs/{_ORG}/docker/containers/nginx/start")
        assert r.status_code == 200

    def test_stop_ok(self, docker_client: TestClient) -> None:
        result = mock.MagicMock()
        result.model_dump.return_value = {
            "container_id": "abc123",
            "operation": "stop",
            "success": True,
            "elapsed_ms": 50,
            "state_before": "running",
            "state_after": "exited",
        }
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_stop",
            return_value=result,
        ):
            r = docker_client.post(f"/api/v1/orgs/{_ORG}/docker/containers/nginx/stop")
        assert r.status_code == 200

    def test_restart_ok(self, docker_client: TestClient) -> None:
        result = mock.MagicMock()
        result.model_dump.return_value = {
            "container_id": "abc123",
            "operation": "restart",
            "success": True,
            "elapsed_ms": 50,
            "state_before": "running",
            "state_after": "running",
        }
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_restart",
            return_value=result,
        ):
            r = docker_client.post(f"/api/v1/orgs/{_ORG}/docker/containers/nginx/restart")
        assert r.status_code == 200

    def test_logs_ok(self, docker_client: TestClient) -> None:
        log_line = mock.MagicMock()
        log_line.model_dump.return_value = {"timestamp": "2026-05-24T00:00:00Z", "message": "line1"}
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_logs",
            return_value=[log_line, log_line],
        ):
            r = docker_client.get(f"/api/v1/orgs/{_ORG}/docker/containers/nginx/logs?tail=50")
        assert r.status_code == 200
        assert len(r.json()["lines"]) == 2

    def test_logs_since_seconds(self, docker_client: TestClient) -> None:
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_logs",
            return_value=[],
        ):
            r = docker_client.get(
                f"/api/v1/orgs/{_ORG}/docker/containers/nginx/logs?tail=10&since_seconds=300"
            )
        assert r.status_code == 200

    def test_logs_oprim_error_502(self, docker_client: TestClient) -> None:
        from oprim._exceptions import OprimError

        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_logs",
            side_effect=OprimError("not found"),
        ):
            r = docker_client.get(f"/api/v1/orgs/{_ORG}/docker/containers/missing/logs")
        assert r.status_code == 502


# ---------------------------------------------------------------------------
# Apps router tests
# ---------------------------------------------------------------------------


class TestAppsRouter:
    def test_list_apps_empty(self, apps_client: TestClient) -> None:
        r = apps_client.get(f"/api/v1/orgs/{_ORG}/apps")
        assert r.status_code == 200
        assert r.json() == []

    def test_get_app_not_found(self, apps_client: TestClient) -> None:
        r = apps_client.get(f"/api/v1/orgs/{_ORG}/apps/{_APP_ID}")
        assert r.status_code == 404

    def test_get_app_found(self, apps_client: TestClient, apps_conn: mock.AsyncMock) -> None:
        apps_conn.fetchrow.return_value = {
            "id": _APP_ID,
            "app_name": "homeassistant",
            "app_version": "2024.1",
            "install_dir": "/tmp/ha",
            "domain": "ha.local",
            "status": "completed",
            "installed_at": "2026-05-20T00:00:00Z",
        }
        r = apps_client.get(f"/api/v1/orgs/{_ORG}/apps/{_APP_ID}")
        assert r.status_code == 200
        assert r.json()["app_name"] == "homeassistant"

    def test_install_accepted(self, apps_client: TestClient, apps_conn: mock.AsyncMock) -> None:
        # install_app_endpoint looks up the project first
        apps_conn.fetchrow.return_value = _project_row()
        with mock.patch("aegis.server.api.routers.apps._run_install"):
            r = apps_client.post(
                f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
                json={
                    "app_name": "nginx",
                    "install_dir": "/tmp/nginx",
                },
            )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "installing"
        assert "install_id" in body

    def test_install_custom_dir(self, apps_client: TestClient, apps_conn: mock.AsyncMock) -> None:
        apps_conn.fetchrow.return_value = _project_row()
        apps_conn.fetchval.return_value = _APP_ID
        with mock.patch("aegis.server.api.routers.apps._run_install"):
            r = apps_client.post(
                f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
                json={"app_name": "redis", "install_dir": "/opt/redis"},
            )
        assert r.status_code == 202

    def test_uninstall_ok(self, apps_client: TestClient) -> None:
        r = apps_client.delete(f"/api/v1/orgs/{_ORG}/apps/{_APP_ID}")
        assert r.status_code == 204

    def test_uninstall_not_found(self, apps_client: TestClient, apps_conn: mock.AsyncMock) -> None:
        apps_conn.execute.return_value = "DELETE 0"
        r = apps_client.delete(f"/api/v1/orgs/{_ORG}/apps/{_APP_ID}")
        assert r.status_code == 404

    def test_list_apps_returns_expected_fields(
        self, apps_client: TestClient, apps_conn: mock.AsyncMock
    ) -> None:
        apps_conn.fetch.return_value = [
            {
                "id": _APP_ID,
                "app_name": "homeassistant",
                "app_version": "2024.1",
                "install_dir": "/opt/ha",
                "domain": "ha.local",
                "status": "completed",
                "installed_at": "2026-06-06T00:00:00Z",
            }
        ]
        r = apps_client.get(f"/api/v1/orgs/{_ORG}/apps")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["app_name"] == "homeassistant"
        assert items[0]["status"] == "completed"

    def test_install_project_not_in_org_returns_404(
        self, apps_client: TestClient, apps_conn: mock.AsyncMock
    ) -> None:
        other_org = uuid.UUID("99999999-9999-9999-9999-999999999999")
        apps_conn.fetchrow.return_value = {
            **_project_row(),
            "org_id": other_org,
        }
        r = apps_client.post(
            f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
            json={"app_name": "nginx", "install_dir": "/tmp/nginx"},
        )
        assert r.status_code == 404

    def test_install_blank_install_dir_rejected(self, apps_client: TestClient) -> None:
        r = apps_client.post(
            f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
            json={"app_name": "nginx", "install_dir": "   "},
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Domains router tests
# ---------------------------------------------------------------------------


class TestDomainsRouter:
    def test_list_domains_empty(self, domains_client: TestClient) -> None:
        r = domains_client.get(f"/api/v1/orgs/{_ORG}/domains")
        assert r.status_code == 200
        assert r.json() == []

    def test_register_domain_edge_success(self, domains_client: TestClient) -> None:
        edge_resp = mock.MagicMock()
        edge_resp.is_success = True
        edge_resp.status_code = 201

        with mock.patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value.post.return_value = edge_resp
            r = domains_client.post(
                f"/api/v1/orgs/{_ORG}/domains?project_id={_PROJ}",
                json={
                    "domain": "ha.example.com",
                    "target_url": "http://localhost:8123",
                },
            )

        assert r.status_code == 201
        body = r.json()
        assert body["domain"] == "ha.example.com"
        assert body["edge_registered"] is True

    def test_register_domain_edge_unavailable(self, domains_client: TestClient) -> None:
        with mock.patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value.post.side_effect = httpx_request_error()
            r = domains_client.post(
                f"/api/v1/orgs/{_ORG}/domains?project_id={_PROJ}",
                json={
                    "domain": "ha.example.com",
                    "target_url": "http://localhost:8123",
                },
            )

        # Still 201 — DB write proceeds even if edge is unreachable
        assert r.status_code == 201
        body = r.json()
        assert body["edge_registered"] is False
        assert body["edge_error"] is not None

    def test_register_domain_edge_error_response(self, domains_client: TestClient) -> None:
        edge_resp = mock.MagicMock()
        edge_resp.is_success = False
        edge_resp.status_code = 400
        edge_resp.text = "bad domain"

        with mock.patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value.post.return_value = edge_resp
            r = domains_client.post(
                f"/api/v1/orgs/{_ORG}/domains?project_id={_PROJ}",
                json={"domain": "bad", "target_url": "http://x"},
            )

        assert r.status_code == 201
        assert r.json()["edge_registered"] is False

    def test_delete_domain_ok(self, domains_client: TestClient) -> None:
        r = domains_client.delete(f"/api/v1/orgs/{_ORG}/domains/ha.example.com")
        assert r.status_code == 204

    def test_delete_domain_not_found(
        self, domains_client: TestClient, domains_conn: mock.AsyncMock
    ) -> None:
        domains_conn.execute.return_value = "DELETE 0"
        r = domains_client.delete(f"/api/v1/orgs/{_ORG}/domains/nope.example.com")
        assert r.status_code == 404

    def test_register_tls_off(self, domains_client: TestClient) -> None:
        edge_resp = mock.MagicMock()
        edge_resp.is_success = True
        edge_resp.status_code = 201
        with mock.patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__.return_value.post.return_value = edge_resp
            r = domains_client.post(
                f"/api/v1/orgs/{_ORG}/domains?project_id={_PROJ}",
                json={
                    "domain": "local.test",
                    "target_url": "http://localhost:9000",
                    "tls_mode": "off",
                },
            )
        assert r.status_code == 201


def httpx_request_error() -> Exception:
    import httpx  # noqa: PLC0415

    return httpx.ConnectError("refused")
