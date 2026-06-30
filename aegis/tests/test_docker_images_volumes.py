"""Tests for image management + network/volume listing/deletion (audit #11/#12).

These endpoints didn't exist (oprim had the functions but nothing exposed them).
Verify they call oprim on the resolved docker_host and enforce RBAC.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import docker as docker_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _client(role: str = "owner") -> TestClient:
    fa = FastAPI()
    fa.include_router(docker_router.router)

    async def _user() -> UserContext:
        return UserContext(
            user_id=uuid.uuid4(), email="t@x.com",
            orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)],
        )

    async def _conn() -> Generator[mock.AsyncMock, None, None]:
        yield mock.AsyncMock()

    fa.dependency_overrides[get_current_user] = _user
    fa.dependency_overrides[get_db_conn] = _conn
    return TestClient(fa, raise_server_exceptions=False)


def test_list_images_calls_oprim():
    c = _client()
    with mock.patch(
        "aegis.server.api.routers.docker.docker_image_list", return_value=[{"id": "sha256:x"}]
    ) as m:
        r = c.get(f"/api/v1/orgs/{_ORG}/docker/images")
    assert r.status_code == 200 and r.json() == [{"id": "sha256:x"}]
    # no node_id → docker_host omitted so oprim uses its own default
    assert "docker_host" not in m.call_args.kwargs


def test_pull_image_calls_oprim():
    c = _client()
    res = mock.MagicMock()
    res.model_dump.return_value = {"image": "nginx", "tag": "1.25", "pulled": True}
    with mock.patch("aegis.server.api.routers.docker.docker_image_pull", return_value=res) as m:
        r = c.post(f"/api/v1/orgs/{_ORG}/docker/images/pull", json={"image": "nginx", "tag": "1.25"})
    assert r.status_code == 200
    assert m.call_args.kwargs["image"] == "nginx" and m.call_args.kwargs["tag"] == "1.25"


def test_delete_volume_calls_oprim():
    c = _client()
    with mock.patch(
        "aegis.server.api.routers.docker.docker_volume_delete", return_value={"deleted": True}
    ) as m:
        r = c.delete(f"/api/v1/orgs/{_ORG}/docker/volumes/myvol?force=true")
    assert r.status_code == 200
    assert m.call_args.kwargs["name"] == "myvol" and m.call_args.kwargs["force"] is True


def test_list_volumes_and_networks():
    c = _client()
    with (
        mock.patch("aegis.server.api.routers.docker.docker_volume_list", return_value=[]),
        mock.patch("aegis.server.api.routers.docker.docker_network_list", return_value=[]),
    ):
        assert c.get(f"/api/v1/orgs/{_ORG}/docker/volumes").status_code == 200
        assert c.get(f"/api/v1/orgs/{_ORG}/docker/networks").status_code == 200


def test_prune_requires_operator_role():
    """viewer must not be able to prune (destructive)."""
    c = _client(role="viewer")
    r = c.post(f"/api/v1/orgs/{_ORG}/docker/system/prune")
    assert r.status_code == 403


def test_delete_image_requires_operator_role():
    c = _client(role="viewer")
    r = c.delete(f"/api/v1/orgs/{_ORG}/docker/images/nginx:latest")
    assert r.status_code == 403
