"""Tests for git-deploy: validation, clone+build orchestration, router."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest import mock

import pytest

from aegis.server.services import git_deploy as gd


@pytest.fixture(autouse=True)
def _stop_patches():
    """Ensure patch.start() calls in _client are always undone (no cross-test leak)."""
    yield
    mock.patch.stopall()


def test_validate_rejects_bad_inputs():
    with pytest.raises(ValueError, match="http"):
        gd._validate("git@github.com:x/y.git", "app", None, None)
    with pytest.raises(ValueError, match="app_name"):
        gd._validate("https://h/r.git", "Bad Name!", None, None)
    with pytest.raises(ValueError, match="subdir"):
        gd._validate("https://h/r.git", "app", None, "../etc")
    gd._validate("https://github.com/o/r.git", "my-app", "main", "svc/api")  # ok


@pytest.mark.asyncio
async def test_build_and_deploy_happy_path(tmp_path):
    fake_img = mock.MagicMock()

    def fake_clone(cmd, **kw):
        # git clone <...> dest → create the dest dir with a Dockerfile
        dest = cmd[-1]
        import os
        os.makedirs(dest, exist_ok=True)
        open(os.path.join(dest, "Dockerfile"), "w").write("FROM alpine\n")
        return mock.MagicMock(returncode=0, stderr="", stdout="")

    with (
        mock.patch("subprocess.run", side_effect=fake_clone),
        mock.patch("docker.from_env") as dfe,
        mock.patch("oprim.docker_container_create") as create,
        mock.patch("oprim.docker_container_start") as start,
    ):
        dfe.return_value.images.build.return_value = (fake_img, [])
        tag = await gd.build_and_deploy_from_git(
            repo_url="https://github.com/o/r.git", branch="main", app_name="webapp",
            subdir=None, ports=[8080], env=[{"name": "K", "value": "V"}],
            docker_host="unix:///x.sock", build_root=str(tmp_path))

    assert tag == "aegis-git/webapp:latest"
    dfe.return_value.images.build.assert_called_once()
    assert create.call_args.kwargs["image"] == "aegis-git/webapp:latest"
    assert create.call_args.kwargs["ports"] == {"8080/tcp": 8080}
    assert create.call_args.kwargs["env"] == {"K": "V"}
    start.assert_called_once()


@pytest.mark.asyncio
async def test_build_fails_without_dockerfile(tmp_path):
    def fake_clone(cmd, **kw):
        import os
        os.makedirs(cmd[-1], exist_ok=True)  # no Dockerfile
        return mock.MagicMock(returncode=0, stderr="", stdout="")

    with mock.patch("subprocess.run", side_effect=fake_clone):
        with pytest.raises(RuntimeError, match="no Dockerfile"):
            await gd.build_and_deploy_from_git(
                repo_url="https://h/r.git", branch=None, app_name="x", subdir=None,
                ports=None, env=None, docker_host="unix:///x", build_root=str(tmp_path))


@pytest.mark.asyncio
async def test_clone_failure_raises(tmp_path):
    with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=128, stderr="not found", stdout="")):
        with pytest.raises(RuntimeError, match="git clone failed"):
            await gd.build_and_deploy_from_git(
                repo_url="https://h/r.git", branch=None, app_name="x", subdir=None,
                ports=None, env=None, docker_host="unix:///x", build_root=str(tmp_path))


# ---- router ----
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from aegis.server.api.deps import get_db_conn  # noqa: E402
from aegis.server.api.routers import git_deploy as gdr  # noqa: E402
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user  # noqa: E402

_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _client(role="owner", project_org=_ORG):
    conn = mock.AsyncMock()
    conn.fetchval.return_value = uuid.uuid4()
    fa = FastAPI()
    fa.include_router(gdr.router)

    async def _user():
        return UserContext(user_id=uuid.uuid4(), email="t@x.com",
                           orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)])

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_current_user] = _user
    fa.dependency_overrides[get_db_conn] = _conn
    proj = mock.MagicMock(org_id=project_org)
    gdr_patches = [
        mock.patch.object(gdr.ProjectRepository, "get_by_id", mock.AsyncMock(return_value=proj)),
        mock.patch.object(gdr, "_run_git_deploy", mock.AsyncMock()),
    ]
    for p in gdr_patches:
        p.start()
    return TestClient(fa, raise_server_exceptions=False), conn


def test_router_accepts_deploy():
    c, conn = _client()
    pid = uuid.uuid4()
    r = c.post(f"/api/v1/orgs/{_ORG}/git-deploy?project_id={pid}",
               json={"repo_url": "https://github.com/o/r.git", "app_name": "webapp", "ports": [8080]})
    assert r.status_code == 202 and r.json()["status"] == "building"


def test_router_viewer_forbidden():
    c, _ = _client(role="viewer")
    r = c.post(f"/api/v1/orgs/{_ORG}/git-deploy?project_id={uuid.uuid4()}",
               json={"repo_url": "https://h/r.git", "app_name": "x"})
    assert r.status_code == 403
