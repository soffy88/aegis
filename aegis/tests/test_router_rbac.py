"""C1-4 RBAC gate tests for org-scoped routers.

Tests that:
- Unauthenticated requests → 401
- Authenticated but not member of target org → 403
- Role missing required permission → 403
- Role has required permission → 2xx

Covers docker, apps, events, alerts, projects, runbooks, domains, store.
health router is public (no auth) — tested separately.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import alerts as alerts_router
from aegis.server.api.routers import apps as apps_router
from aegis.server.api.routers import docker as docker_router
from aegis.server.api.routers import domains as domains_router
from aegis.server.api.routers import events as events_router
from aegis.server.api.routers import projects as projects_router
from aegis.server.api.routers import runbooks as runbooks_router
from aegis.server.api.routers import store as store_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ORG = uuid.UUID("99999999-9999-9999-9999-999999999999")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# ---------------------------------------------------------------------------
# User factories
# ---------------------------------------------------------------------------


def _user(role: str, org_id: uuid.UUID = _ORG) -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=org_id, slug="test-org", role=role)],
    )


def _project_row() -> dict:
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
# Mini-app factory
# ---------------------------------------------------------------------------


def _make_fa(*routers: object) -> FastAPI:
    fa = FastAPI()
    for r in routers:
        fa.include_router(r)  # type: ignore[arg-type]
    return fa


def _add_db(fa: FastAPI, conn: mock.AsyncMock) -> None:
    async def _override() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _override


def _set_user(fa: FastAPI, role: str, org_id: uuid.UUID = _ORG) -> None:
    u = _user(role, org_id)

    async def _override() -> UserContext:
        return u

    fa.dependency_overrides[get_current_user] = _override


# ---------------------------------------------------------------------------
# §1 Unauthenticated → 401 (no override = real OAuth2 scheme → 401)
# ---------------------------------------------------------------------------


class TestUnauthenticated:
    """All org-scoped endpoints must reject requests with no Bearer token.

    Routers that also depend on get_db_conn get a stub DB mock so that only
    the missing auth causes the failure (otherwise the uninitialized pool
    raises a 500 first, masking the 401).
    """

    def _client_no_auth(self, *routers: object) -> TestClient:
        fa = _make_fa(*routers)
        # Stub DB so only auth is absent
        _add_db(fa, mock.AsyncMock())
        # NO get_current_user override — OAuth2PasswordBearer returns 401
        return TestClient(fa, raise_server_exceptions=False)

    def test_docker_no_auth(self) -> None:
        c = self._client_no_auth(docker_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/docker/containers/nginx")
        assert r.status_code == 401

    def test_apps_no_auth(self) -> None:
        c = self._client_no_auth(apps_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/apps")
        assert r.status_code == 401

    def test_events_no_auth(self) -> None:
        c = self._client_no_auth(events_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/events")
        assert r.status_code == 401

    def test_alerts_no_auth(self) -> None:
        c = self._client_no_auth(alerts_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/alerts")
        assert r.status_code == 401

    def test_projects_no_auth(self) -> None:
        c = self._client_no_auth(projects_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects")
        assert r.status_code == 401

    def test_runbooks_no_auth(self) -> None:
        c = self._client_no_auth(runbooks_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/runbooks")
        assert r.status_code == 401

    def test_store_no_auth(self) -> None:
        c = self._client_no_auth(store_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/store")
        assert r.status_code == 401

    def test_domains_no_auth(self) -> None:
        c = self._client_no_auth(domains_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/domains")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# §2 Wrong org → 403 (authenticated but not a member of the path org)
# ---------------------------------------------------------------------------


class TestWrongOrg:
    """User authenticated for OTHER_ORG must be rejected on _ORG paths."""

    @pytest.fixture
    def conn(self) -> mock.AsyncMock:
        m = mock.AsyncMock()
        m.fetch.return_value = []
        m.fetchrow.return_value = None
        return m

    def _client(self, conn: mock.AsyncMock, *routers: object) -> TestClient:
        fa = _make_fa(*routers)
        _set_user(fa, "owner", org_id=_OTHER_ORG)  # member of OTHER_ORG, not _ORG
        _add_db(fa, conn)
        return TestClient(fa, raise_server_exceptions=False)

    def test_docker_wrong_org(self, conn: mock.AsyncMock) -> None:
        c = self._client(conn, docker_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/docker/containers/nginx")
        assert r.status_code == 403

    def test_apps_wrong_org(self, conn: mock.AsyncMock) -> None:
        c = self._client(conn, apps_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/apps")
        assert r.status_code == 403

    def test_events_wrong_org(self, conn: mock.AsyncMock) -> None:
        c = self._client(conn, events_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/events")
        assert r.status_code == 403

    def test_projects_wrong_org(self, conn: mock.AsyncMock) -> None:
        c = self._client(conn, projects_router.router)
        r = c.get(f"/api/v1/orgs/{_ORG}/projects")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# §3 Viewer: read allowed, write blocked
# ---------------------------------------------------------------------------


class TestViewerRole:
    """viewer can read (VIEW_PROJECT/VIEW_EVENTS) but must be blocked on writes."""

    @pytest.fixture
    def conn(self) -> mock.AsyncMock:
        m = mock.AsyncMock()
        m.fetch.return_value = []
        m.fetchrow.return_value = _project_row()
        m.fetchval.return_value = _PROJ
        return m

    @pytest.fixture
    def client(self, conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
        fa = _make_fa(
            docker_router.router,
            apps_router.router,
            events_router.router,
            projects_router.router,
        )
        _set_user(fa, "viewer")
        _add_db(fa, conn)
        with TestClient(fa, raise_server_exceptions=False) as c:
            yield c

    def test_viewer_can_list_projects(self, client: TestClient) -> None:
        r = client.get(f"/api/v1/orgs/{_ORG}/projects")
        assert r.status_code == 200

    def test_viewer_can_list_events(self, client: TestClient) -> None:
        r = client.get(f"/api/v1/orgs/{_ORG}/events")
        assert r.status_code == 200

    def test_viewer_can_inspect_container(self, client: TestClient) -> None:
        result = mock.MagicMock()
        result.model_dump.return_value = {"container_id": "x", "state": "running"}
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_inspect", return_value=result
        ):
            r = client.get(f"/api/v1/orgs/{_ORG}/docker/containers/nginx")
        assert r.status_code == 200

    def test_viewer_cannot_install_app(self, client: TestClient) -> None:
        """INSTALL_APP requires member+; viewer must get 403."""
        r = client.post(
            f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
            json={"app_name": "nginx", "install_dir": "/tmp/x"},
        )
        assert r.status_code == 403

    def test_viewer_cannot_create_project(self, client: TestClient) -> None:
        """CREATE_PROJECT requires member+."""
        r = client.post(
            f"/api/v1/orgs/{_ORG}/projects",
            json={"slug": "new-proj", "name": "New", "display_name": "New"},
        )
        assert r.status_code == 403

    def test_viewer_cannot_start_container(self, client: TestClient) -> None:
        """TRIGGER_AUTOHEAL requires operator+; viewer must get 403."""
        r = client.post(f"/api/v1/orgs/{_ORG}/docker/containers/nginx/start")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# §4 Operator: can trigger autoheal, cannot install app
# ---------------------------------------------------------------------------


class TestOperatorRole:
    @pytest.fixture
    def conn(self) -> mock.AsyncMock:
        m = mock.AsyncMock()
        m.fetch.return_value = []
        m.fetchrow.return_value = _project_row()
        return m

    @pytest.fixture
    def client(self, conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
        fa = _make_fa(docker_router.router, apps_router.router)
        _set_user(fa, "operator")
        _add_db(fa, conn)
        with TestClient(fa, raise_server_exceptions=False) as c:
            yield c

    def test_operator_can_start_container(self, client: TestClient) -> None:
        result = mock.MagicMock()
        result.model_dump.return_value = {"success": True}
        with mock.patch(
            "aegis.server.api.routers.docker.docker_container_start", return_value=result
        ):
            r = client.post(f"/api/v1/orgs/{_ORG}/docker/containers/nginx/start")
        assert r.status_code == 200

    def test_operator_cannot_install_app(self, client: TestClient) -> None:
        """INSTALL_APP requires member+; operator must get 403."""
        r = client.post(
            f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
            json={"app_name": "nginx", "install_dir": "/tmp/x"},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# §5 Member: can install + create project
# ---------------------------------------------------------------------------


class TestMemberRole:
    @pytest.fixture
    def conn(self) -> mock.AsyncMock:
        m = mock.AsyncMock()
        m.fetch.return_value = []
        m.fetchrow.return_value = _project_row()
        m.fetchval.return_value = uuid.uuid4()
        return m

    @pytest.fixture
    def client(self, conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
        fa = _make_fa(apps_router.router, projects_router.router)
        _set_user(fa, "member")
        _add_db(fa, conn)
        with TestClient(fa, raise_server_exceptions=False) as c:
            yield c

    def test_member_can_install_app(self, client: TestClient) -> None:
        with mock.patch("aegis.server.api.routers.apps._run_install"):
            r = client.post(
                f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
                json={"app_name": "nginx", "install_dir": "/tmp/nginx"},
            )
        assert r.status_code == 202

    def test_member_can_create_project(self, client: TestClient, conn: mock.AsyncMock) -> None:
        conn.fetchrow.return_value = None  # no existing slug conflict
        conn.fetchval.return_value = None
        # create_project uses conn directly for insert; mock it
        from aegis.server.models.project import Project  # noqa: PLC0415

        created = Project(
            id=_PROJ,
            org_id=_ORG,
            slug="brand-new",
            name="Brand New",
            display_name="Brand New",
            environment="prod",
            docker_labels=None,
            config=None,
            archived_at=None,
            created_at=datetime(2026, 1, 1),
        )
        with (
            mock.patch(
                "aegis.server.repositories.project_repo.ProjectRepository.get_by_org_and_slug",
                new_callable=mock.AsyncMock,
                return_value=None,
            ),
            mock.patch(
                "aegis.server.repositories.project_repo.ProjectRepository.create",
                new_callable=mock.AsyncMock,
                return_value=created,
            ),
        ):
            r = client.post(
                f"/api/v1/orgs/{_ORG}/projects",
                json={"slug": "brand-new", "name": "Brand New", "display_name": "Brand New"},
            )
        assert r.status_code == 201

    def test_member_cannot_delete_project(self, client: TestClient) -> None:
        """DELETE_PROJECT requires admin+."""
        r = client.delete(f"/api/v1/orgs/{_ORG}/projects/{_PROJ}")
        assert r.status_code == 403
