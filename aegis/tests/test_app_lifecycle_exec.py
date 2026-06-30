"""Tests for real app upgrade/rollback execution + install image resolution.

Audit P0 #5/#6: _run_app_lifecycle was a log-only stub that always marked apps
'active'; install ignored the catalog image. These verify the lifecycle now
dispatches the real omodul call and reports truthful status, and that the install
path resolves the container image from the store catalog.
"""

from __future__ import annotations

import uuid
from unittest import mock

import pytest

from aegis.server.api.routers import apps as apps_router
from aegis.server.api.routers.apps import _run_app_lifecycle
from aegis.server.api.routers.store import find_catalog_app

_APP = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _pool_yielding(conn: mock.AsyncMock) -> mock.MagicMock:
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


@pytest.mark.asyncio
async def test_upgrade_dispatches_and_marks_active_on_completed():
    conn = mock.AsyncMock()
    dispatch = mock.AsyncMock(return_value={"status": "completed"})
    with (
        mock.patch.object(apps_router, "_dispatch_upgrade", dispatch),
        mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)),
    ):
        await _run_app_lifecycle(
            omodul_name="upgrade_self_hosted_app", app_name="grafana",
            target_version="11.0", install_id=_APP, org_id=_ORG, current_version="10.0",
        )

    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["target_version"] == "11.0"
    upd = conn.execute.await_args
    assert "status = $2" in upd.args[0] and upd.args[2] == "active"


@pytest.mark.asyncio
async def test_upgrade_marks_failed_when_omodul_not_completed():
    conn = mock.AsyncMock()
    dispatch = mock.AsyncMock(return_value={"status": "failed", "error": "no image"})
    with (
        mock.patch.object(apps_router, "_dispatch_upgrade", dispatch),
        mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)),
    ):
        await _run_app_lifecycle(
            omodul_name="upgrade_self_hosted_app", app_name="grafana",
            target_version="11.0", install_id=_APP, org_id=_ORG,
        )

    assert conn.execute.await_args.args[2] == "failed"


@pytest.mark.asyncio
async def test_rollback_invokes_real_rollback_and_marks_active():
    conn = mock.AsyncMock()
    rb = mock.MagicMock(return_value={"status": "completed"})
    with (
        mock.patch.object(apps_router, "_run_rollback", rb),
        mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)),
    ):
        await _run_app_lifecycle(
            omodul_name="rollback_app", app_name="grafana",
            target_version="10.0", install_id=_APP, org_id=_ORG,
        )

    rb.assert_called_once()
    assert rb.call_args.kwargs["rollback_to_version"] == "10.0"
    assert conn.execute.await_args.args[2] == "active"


@pytest.mark.asyncio
async def test_lifecycle_marks_failed_on_exception():
    conn = mock.AsyncMock()
    boom = mock.AsyncMock(side_effect=RuntimeError("dispatch blew up"))
    with (
        mock.patch.object(apps_router, "_dispatch_upgrade", boom),
        mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)),
    ):
        await _run_app_lifecycle(
            omodul_name="upgrade_self_hosted_app", app_name="grafana",
            target_version="11.0", install_id=_APP, org_id=_ORG,
        )

    assert conn.execute.await_args.args[2] == "failed"


def test_find_catalog_app_resolves_builtin_image():
    """A known builtin slug resolves to a real container image."""
    entry = find_catalog_app("uptime-kuma")
    assert entry is not None
    assert entry["image"].startswith("louislam/uptime-kuma")
    assert find_catalog_app("does-not-exist") is None
