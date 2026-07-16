"""ADR-004 P1 — managed sites: create/delete track a `sites` row, audit the
action, and go through obase.docker primitives; the probe loop records health.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import obase.docker as odocker
import pytest

from aegis.server.api.routers import apps as apps_router
from aegis.server.api.routers import websites as w
from aegis.server.api.routers.websites import (
    ScaffoldRequest,
    WebsiteRequest,
    _probe_sites,
    create_website,
    delete_website,
    scaffold_site,
)

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _user() -> mock.Mock:
    return mock.Mock(user_id=uuid.UUID("99999999-9999-9999-9999-999999999999"))


def _pool_yielding(conn: mock.AsyncMock) -> mock.MagicMock:
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


@pytest.fixture
def docker_ok() -> Iterator[mock.AsyncMock]:
    """Patch the docker primitives + host-side helpers so create() is DB-only.

    Yields the patched `record_audit` mock so tests can assert the audit write.
    """
    with (
        mock.patch.object(odocker, "docker_container_create", mock.Mock()),
        mock.patch.object(odocker, "docker_container_start", mock.Mock()),
        mock.patch.object(apps_router, "_pick_free_host_port", return_value=8100),
        mock.patch.object(w, "_caddy_del_domain", mock.Mock()),
        mock.patch.object(w, "_dcmd", mock.Mock()),
        mock.patch.object(w, "record_audit", mock.AsyncMock()) as audit,
    ):
        yield audit


@pytest.mark.asyncio
async def test_create_static_tracks_row_and_audits(
    tmp_path: Path, docker_ok: mock.AsyncMock
) -> None:
    conn = mock.AsyncMock()
    req = WebsiteRequest(name="blog", root_dir=str(tmp_path), php=False)

    with mock.patch("aegis.server.services.files._safe", return_value=tmp_path):
        out = await create_website(_ORG, req, conn=conn, user=_user())

    # obase primitive used (not raw docker run), with the static image + ro bind
    create_kwargs = odocker.docker_container_create.call_args.kwargs
    assert create_kwargs["image"] == "nginx:alpine"
    assert create_kwargs["labels"]["aegis.managed"] == "true"
    assert create_kwargs["volumes"][str(tmp_path)]["mode"] == "ro"
    odocker.docker_container_start.assert_called_once()

    # row upserted + audit written
    assert "INSERT INTO sites" in conn.execute.await_args.args[0]
    docker_ok.assert_awaited_once()
    assert docker_ok.await_args.kwargs["action"] == "site.created"
    assert out["runtime"] == "static"
    assert out["port"] == 8100


@pytest.mark.asyncio
async def test_create_php_selects_php_image(tmp_path: Path, docker_ok: mock.AsyncMock) -> None:
    conn = mock.AsyncMock()
    req = WebsiteRequest(name="shop", root_dir=str(tmp_path), php=True)

    with mock.patch("aegis.server.services.files._safe", return_value=tmp_path):
        out = await create_website(_ORG, req, conn=conn, user=_user())

    assert odocker.docker_container_create.call_args.kwargs["image"] == "php:8.3-apache"
    assert out["runtime"] == "php"


@pytest.mark.asyncio
async def test_create_nextjs_builds_then_serves_out(
    tmp_path: Path, docker_ok: mock.AsyncMock
) -> None:
    conn = mock.AsyncMock()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    req = WebsiteRequest(name="app", root_dir=str(tmp_path), runtime="nextjs-oui")

    with (
        mock.patch("aegis.server.services.files._safe", return_value=tmp_path),
        mock.patch.object(w, "_build_nextjs", return_value=out_dir) as build,
    ):
        res = await create_website(_ORG, req, conn=conn, user=_user())

    build.assert_called_once()  # one-shot build ran
    create_kwargs = odocker.docker_container_create.call_args.kwargs
    assert create_kwargs["image"] == "nginx:alpine"  # served as static
    assert str(out_dir) in create_kwargs["volumes"]  # serves the build output, not src
    assert res["runtime"] == "nextjs-oui"


@pytest.mark.asyncio
async def test_scaffold_endpoint_writes_and_audits(tmp_path: Path) -> None:
    conn = mock.AsyncMock()
    req = ScaffoldRequest(name="blog", parent_dir=str(tmp_path), template="static")

    with (
        mock.patch("aegis.server.services.files._safe", return_value=tmp_path),
        mock.patch.object(w, "record_audit", mock.AsyncMock()) as audit,
    ):
        out = await scaffold_site(_ORG, req, conn=conn, user=_user())

    assert (tmp_path / "blog" / "index.html").exists()
    assert out["runtime"] == "static"
    assert out["dir"] == str(tmp_path / "blog")
    assert audit.await_args.kwargs["action"] == "site.scaffolded"


@pytest.mark.asyncio
async def test_delete_removes_row_and_audits() -> None:
    conn = mock.AsyncMock()
    with (
        mock.patch.object(w, "_caddy_del_domain", mock.Mock()),
        mock.patch.object(w, "_dcmd", mock.Mock()),
        mock.patch.object(w, "record_audit", mock.AsyncMock()) as audit,
    ):
        await delete_website(_ORG, "blog", conn=conn, user=_user())

    assert "DELETE FROM sites" in conn.execute.await_args.args[0]
    assert audit.await_args.kwargs["action"] == "site.deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "health", "exp_up", "exp_status"),
    [
        ("running", "healthy", True, "running"),
        ("running", "unhealthy", False, "unhealthy"),
        ("exited", "none", False, "exited"),
    ],
)
async def test_probe_records_health(state: str, health: str, exp_up: bool, exp_status: str) -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"id": uuid.uuid4(), "container": "website-blog"}]
    info = SimpleNamespace(state=state, health=health)
    with (
        mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)),
        mock.patch.object(odocker, "docker_container_inspect", return_value=info),
    ):
        n = await _probe_sites()

    assert n == 1
    args = conn.execute.await_args.args
    assert args[1] is exp_up
    assert args[2] == exp_status


@pytest.mark.asyncio
async def test_probe_marks_error_when_container_missing() -> None:
    conn = mock.AsyncMock()
    conn.fetch.return_value = [{"id": uuid.uuid4(), "container": "website-gone"}]
    with (
        mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)),
        mock.patch.object(
            odocker, "docker_container_inspect", side_effect=RuntimeError("no such container")
        ),
    ):
        await _probe_sites()

    args = conn.execute.await_args.args
    assert args[1] is False
    assert args[2] == "error"
    assert "no such container" in args[3]
