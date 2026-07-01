"""Tests for the real docker-run install path (catalog spec -> container_create)."""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from aegis.server.api.routers import apps as apps_router
from aegis.server.api.routers.apps import InstallAppRequest, _build_container_spec, _run_install


def test_build_container_spec_translates_catalog():
    body = InstallAppRequest(
        app_name="uptime-kuma",
        image_to_pull="louislam/uptime-kuma:1.23.16",
        ports=[{"container_port": 3001, "protocol": "tcp", "label": "Web"}],
        env=[{"name": "TZ", "value": "UTC"}],
        mounts=[{"target": "/app/data", "volume_name": "uk-data"}],
    )
    spec = _build_container_spec(body)
    assert spec["ports"] == {"3001/tcp": 3001}
    assert spec["env"] == {"TZ": "UTC"}
    assert spec["volumes"] == {"uk-data": {"bind": "/app/data", "mode": "rw"}}


def test_empty_spec_is_none():
    spec = _build_container_spec(InstallAppRequest(app_name="x", image_to_pull="nginx"))
    assert spec == {"ports": None, "env": None, "volumes": None, "command": None}


@pytest.mark.asyncio
async def test_run_install_creates_and_starts_container():
    conn = mock.AsyncMock()
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    body = InstallAppRequest(app_name="nginx", image_to_pull="nginx:alpine",
                             ports=[{"container_port": 80, "protocol": "tcp"}])
    with (
        mock.patch.object(apps_router, "get_pool", return_value=pool),
        mock.patch("oprim.docker_image_pull") as pull,
        mock.patch("oprim.docker_container_create") as create,
        mock.patch("oprim.docker_container_start") as start,
    ):
        await _run_install(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "trc", body)

    pull.assert_called_once()
    create.assert_called_once()
    assert create.call_args.kwargs["image"] == "nginx:alpine"
    assert create.call_args.kwargs["ports"] == {"80/tcp": 80}
    start.assert_called_once_with(container_id="nginx", docker_host=body.docker_host)
    # DB marked completed
    upd = " ".join(c.args[0] for c in conn.execute.await_args_list)
    assert "installed_apps" in upd


@pytest.mark.asyncio
async def test_run_install_marks_failed_without_image():
    conn = mock.AsyncMock()
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    body = InstallAppRequest(app_name="x", image_to_pull=None)
    with mock.patch.object(apps_router, "get_pool", return_value=pool):
        await _run_install(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "trc", body)
    # first UPDATE sets failed
    assert conn.execute.await_args_list[0].args[1] == "failed"
