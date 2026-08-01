"""Tests for install-on-node + image tracking + uninstall teardown (A1/A2/B1)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import apps as apps_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PROJ = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_APP = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_NODE = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _user() -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(), email="t@x.com",
        orgs=[OrgInToken(org_id=_ORG, slug="o", role="owner")],
    )


def _client(conn: mock.AsyncMock) -> TestClient:
    fa = FastAPI()
    fa.include_router(apps_router.router)
    fa.dependency_overrides[get_current_user] = _user

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _conn
    return TestClient(fa, raise_server_exceptions=False)


def _project_row():
    return {
        "id": _PROJ, "org_id": _ORG, "slug": "p", "name": "P", "display_name": "P",
        "environment": "prod", "docker_labels": None, "config": None,
        "archived_at": None, "created_at": datetime(2026, 1, 1),
    }


def test_uninstall_stops_container_before_delete():
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"app_name": "grafana", "domain": None}
    with mock.patch("obase.docker.docker_container_stop") as stop:
        r = _client(conn).delete(f"/api/v1/orgs/{_ORG}/apps/{_APP}")
    assert r.status_code == 204
    assert stop.call_args.kwargs["container_id"] == "grafana"


def test_install_stores_resolved_image():
    """B1: the install INSERT must persist the image column."""
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = _project_row()
    conn.fetchval.return_value = _APP
    with mock.patch("aegis.server.api.routers.apps._run_install"):
        r = _client(conn).post(
            f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
            json={"app_name": "uptime-kuma", "install_dir": "/opt/uk"},
        )
    assert r.status_code == 202
    insert_call = next(c for c in conn.fetchval.await_args_list if "INSERT INTO installed_apps" in c.args[0])
    assert "image" in insert_call.args[0]
    # catalog image resolved for a known builtin slug
    assert insert_call.args[6] and insert_call.args[6].startswith("louislam/uptime-kuma")


def test_install_on_node_routes_to_node_host():
    """A2: node_id resolves the node's host/docker_host for the install."""
    conn = mock.AsyncMock()
    conn.fetchrow.side_effect = [
        _project_row(),
        {"host": "10.0.0.7", "docker_host_url": "tcp://10.0.0.7:2375"},
    ]
    conn.fetchval.return_value = _APP
    with mock.patch("aegis.server.api.routers.apps._run_install") as run:
        r = _client(conn).post(
            f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
            json={"app_name": "nginx", "install_dir": "/opt/n", "node_id": str(_NODE)},
        )
    assert r.status_code == 202
    body = run.call_args.args[4]  # InstallAppRequest
    assert body.target_host == "10.0.0.7"
    assert body.docker_host == "tcp://10.0.0.7:2375"


def test_install_unknown_node_404():
    conn = mock.AsyncMock()
    conn.fetchrow.side_effect = [_project_row(), None]  # project ok, node missing
    conn.fetchval.return_value = _APP
    with mock.patch("aegis.server.api.routers.apps._run_install"):
        r = _client(conn).post(
            f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}",
            json={"app_name": "nginx", "install_dir": "/opt/n", "node_id": str(_NODE)},
        )
    assert r.status_code == 404


def test_uninstall_removes_container_and_keeps_volumes_by_default():
    """A1: stop is not enough — the container must also be rm'd, but the app's
    data volumes survive unless the caller explicitly asks to purge them."""
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"app_name": "grafana", "domain": None}
    with (
        mock.patch("obase.docker.docker_container_stop"),
        mock.patch("obase.docker.docker_container_remove") as rm,
    ):
        r = _client(conn).delete(f"/api/v1/orgs/{_ORG}/apps/{_APP}")
    assert r.status_code == 204
    assert rm.call_args.kwargs["container_id"] == "grafana"
    assert rm.call_args.kwargs["force"] is True
    assert rm.call_args.kwargs["remove_volumes"] is False


def test_uninstall_purge_volumes_opt_in():
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"app_name": "grafana", "domain": None}
    with (
        mock.patch("obase.docker.docker_container_stop"),
        mock.patch("obase.docker.docker_container_remove") as rm,
    ):
        r = _client(conn).delete(f"/api/v1/orgs/{_ORG}/apps/{_APP}?purge_volumes=true")
    assert r.status_code == 204
    assert rm.call_args.kwargs["remove_volumes"] is True


def test_uninstall_removes_caddy_route_when_domain_bound():
    """A1: a bound domain must stop pointing at the removed app."""
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"app_name": "grafana", "domain": "g.example.com"}
    edge = mock.MagicMock()
    with (
        mock.patch("obase.docker.docker_container_stop"),
        mock.patch("obase.docker.docker_container_remove"),
        mock.patch("aegis.server.edge.caddy.get_caddy_edge", return_value=edge),
    ):
        r = _client(conn).delete(f"/api/v1/orgs/{_ORG}/apps/{_APP}")
    assert r.status_code == 204
    from aegis.server.edge.caddy import org_route_id

    edge.remove_route.assert_called_once_with(org_route_id(_ORG, "g.example.com"))


def test_uninstall_survives_docker_failure():
    """Teardown is best-effort: a broken daemon must not strand the DB row."""
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {"app_name": "grafana", "domain": None}
    with (
        mock.patch("obase.docker.docker_container_stop", side_effect=RuntimeError("down")),
        mock.patch("obase.docker.docker_container_remove", side_effect=RuntimeError("down")),
    ):
        r = _client(conn).delete(f"/api/v1/orgs/{_ORG}/apps/{_APP}")
    assert r.status_code == 204
    assert conn.execute.await_args.args[0].startswith("DELETE FROM installed_apps")
