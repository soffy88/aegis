"""Tests for §10/§3.7 config-as-code drift detection."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.services import compose_drift as cd


def _app(name, image, status="running"):
    return {
        "app_name": name,
        "image": image,
        "status": status,
        "org_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
    }


def _container(name, image):
    c = MagicMock()
    c.name = name
    c.image = image
    return c


def test_build_declared_skips_imageless():
    rows = [_app("web", "nginx:1.20"), _app("noimg", None)]
    d = cd.build_declared(rows)
    assert d == {"web": {"image": "nginx:1.20"}}


def test_build_running_strips_leading_slash():
    r = cd.build_running([_container("/web", "nginx:1.25"), _container("", "x")])
    assert r == {"web": {"image": "nginx:1.25"}}


@pytest.mark.asyncio
async def test_scan_in_sync_no_events():
    app = _app("web", "nginx:1.20")
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[app])
    cfg = MagicMock(docker_host="unix:///var/run/docker.sock")
    with (
        patch("obase.docker.docker_container_list", return_value=[_container("web", "nginx:1.20")]),
        patch("aegis.server.persistence.append_event", AsyncMock()) as ev,
    ):
        res = await cd.scan_drift(conn, cfg)
    assert res["in_sync"] is True
    ev.assert_not_awaited()  # 一致 → 不写事件


@pytest.mark.asyncio
async def test_scan_image_drift_writes_event():
    app = _app("web", "nginx:1.20")
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[app])
    cfg = MagicMock(docker_host="unix:///var/run/docker.sock")
    with (
        patch("obase.docker.docker_container_list", return_value=[_container("web", "nginx:1.25")]),
        patch("aegis.server.persistence.append_event", AsyncMock()) as ev,
    ):
        res = await cd.scan_drift(conn, cfg)
    assert res["in_sync"] is False
    assert "web" in res["changed"]
    ev.assert_awaited_once()
    kw = ev.await_args.kwargs
    assert kw["event_type"] == "config.drift" and kw["service"] == "web"
    assert kw["payload"]["kind"] == "image_changed"
    assert kw["payload"]["declared"] == "nginx:1.20" and kw["payload"]["running"] == "nginx:1.25"


@pytest.mark.asyncio
async def test_scan_missing_container_writes_not_running_event():
    app = _app("web", "nginx:1.20")
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[app])
    cfg = MagicMock(docker_host="unix:///var/run/docker.sock")
    with (
        patch("obase.docker.docker_container_list", return_value=[_container("other", "busybox")]),
        patch("aegis.server.persistence.append_event", AsyncMock()) as ev,
    ):
        res = await cd.scan_drift(conn, cfg)
    assert res["in_sync"] is False
    assert "web" in res["added"]  # 声明有但没跑
    kw = ev.await_args.kwargs
    assert kw["payload"]["kind"] == "not_running"


@pytest.mark.asyncio
async def test_scan_docker_unreachable_skips():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[_app("web", "nginx:1.20")])
    cfg = MagicMock(docker_host="unix:///var/run/docker.sock")
    with (
        patch("obase.docker.docker_container_list", side_effect=RuntimeError("no docker")),
        patch("aegis.server.persistence.append_event", AsyncMock()) as ev,
    ):
        res = await cd.scan_drift(conn, cfg)
    assert res["in_sync"] is True and "docker_error" in res  # 跳过,不崩
    ev.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_declared_apps_returns_in_sync():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    res = await cd.scan_drift(conn, MagicMock())
    assert res == {"in_sync": True, "checked": 0}


@pytest.mark.asyncio
async def test_drift_loop_registered_in_cron():
    from aegis.server.orchestration import cron

    scheduled: list[str] = []

    async def _fake_gather(*coros, **_kw):
        for c in coros:
            scheduled.append(getattr(c, "__name__", str(c)))
            c.close()

    with (
        patch.object(cron.asyncio, "gather", side_effect=_fake_gather),
        patch.object(cron, "_acquire_loop_runner_role", AsyncMock(return_value=AsyncMock())),
    ):
        await cron._cron_main(alerter=None)
    assert "_drift_loop" in scheduled
