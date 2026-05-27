"""Tests verifying _run_install logs and records errors properly."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest import mock

import pytest

from aegis.server.api.routers.apps import InstallAppRequest, _run_install


@pytest.mark.asyncio
async def test_run_install_import_error_captured() -> None:
    """If omodul cannot be imported, _run_install should set status=failed
    and write error_detail to event_trail (not silent fail)."""
    install_id = uuid.uuid4()
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()

    body = InstallAppRequest(
        app_name="x",
        project_dir="/tmp/x",
    )

    mock_conn: mock.AsyncMock = mock.AsyncMock()
    mock_pool: mock.MagicMock = mock.MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    with (
        mock.patch(
            "aegis.server.api.routers.apps.get_pool",
            return_value=mock_pool,
        ),
        mock.patch.dict("sys.modules", {"aegis.server.services.install_app": None}),
    ):
        sys.modules.pop("aegis.server.services.install_app", None)
        sys.modules["aegis.server.services.install_app"] = None  # type: ignore[assignment]

        await _run_install(
            install_id=install_id,
            org_id=org_id,
            project_id=project_id,
            trace_id="trc_test",
            body=body,
            data_dir=Path("/tmp"),
        )

    calls = mock_conn.execute.call_args_list + mock_conn.fetchrow.call_args_list
    update_calls = [c for c in calls if c and "UPDATE installed_apps" in str(c[0][0])]
    assert len(update_calls) >= 1
    assert any("failed" in str(c[0]) for c in update_calls)


@pytest.mark.asyncio
async def test_run_install_unexpected_exception_captured() -> None:
    """If install_app raises unexpectedly, error is logged + recorded."""
    install_id = uuid.uuid4()
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()

    body = InstallAppRequest(app_name="x", project_dir="/tmp/x")

    mock_conn: mock.AsyncMock = mock.AsyncMock()
    mock_pool: mock.MagicMock = mock.MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

    with (
        mock.patch(
            "aegis.server.api.routers.apps.get_pool",
            return_value=mock_pool,
        ),
        mock.patch(
            "asyncio.to_thread",
            side_effect=RuntimeError("synthetic bug"),
        ),
    ):
        await _run_install(
            install_id=install_id,
            org_id=org_id,
            project_id=project_id,
            trace_id="trc_test",
            body=body,
            data_dir=Path("/tmp"),
        )

    update_calls = [
        c for c in mock_conn.execute.call_args_list if "UPDATE installed_apps" in str(c[0][0])
    ]
    assert len(update_calls) >= 1
    assert any("failed" in str(c[0]) for c in update_calls)
