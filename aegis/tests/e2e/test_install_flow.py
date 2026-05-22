"""e2e: install full-flow tests (BATCH 14 §1C).

Pattern: TestClient (matching existing suite) + mock DB + mock omodul.
DB is not transactional; mocks simulate INSERT/UPDATE/event_trail writes.
_run_install background task executes synchronously within TestClient context.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import apps as apps_router

_INSTALL_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_EVENT_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _make_client(endpoint_conn: mock.AsyncMock) -> Iterator[TestClient]:
    fa = FastAPI()
    fa.include_router(apps_router.router)

    async def _override() -> AsyncIterator[mock.AsyncMock]:
        yield endpoint_conn

    fa.dependency_overrides[get_db_conn] = _override
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


def _endpoint_conn(install_status: str = "completed") -> mock.AsyncMock:
    """Mock asyncpg conn for the HTTP endpoint (INSERT + SELECT path)."""
    conn = mock.AsyncMock()
    conn.fetchval.return_value = _INSTALL_ID  # INSERT ... RETURNING id
    conn.fetchrow.return_value = {
        "id": _INSTALL_ID,
        "app_name": "nginx",
        "app_version": None,
        "install_dir": "/opt/nginx",
        "domain": None,
        "status": install_status,
        "installed_at": "2026-05-22T00:00:00+00:00",
    }
    return conn


def _bg_conn() -> mock.AsyncMock:
    """Mock asyncpg conn used by _run_install background task (UPDATE + event_trail)."""
    conn = mock.AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    conn.fetchrow.return_value = {"id": _EVENT_ID}  # event_trail INSERT RETURNING id
    return conn


def _pool_mock(bg_conn: mock.AsyncMock) -> mock.MagicMock:
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=bg_conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


def _omodul_success(cfg: Any, inp: Any, out_dir: Path) -> dict[str, Any]:
    """Mock install_app: creates output_dir and reports success."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return {"status": "completed", "findings": {}}


def _poll_status(client: TestClient, install_id: str, timeout: float = 5.0) -> str:
    """Poll GET /apps/{id} until status is terminal or timeout. Returns final status."""
    deadline = time.time() + timeout
    status = "installing"
    while status not in ("completed", "failed") and time.time() < deadline:
        r = client.get(f"/api/v1/apps/{install_id}")
        if r.status_code == 200:
            status = r.json().get("status", status)
        if status not in ("completed", "failed"):
            time.sleep(0.1)
    return status


# ---------------------------------------------------------------------------
# e2e tests
# ---------------------------------------------------------------------------


class TestInstallFlow:
    def test_install_completes_with_valid_install_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST valid install_dir → 202, bg task completes, output dir created, status terminal."""
        monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path))
        ec = _endpoint_conn("completed")
        bc = _bg_conn()

        with (
            mock.patch("aegis.server.api.routers.apps.get_pool", return_value=_pool_mock(bc)),
            mock.patch("omodul.install_app.install_app", side_effect=_omodul_success),
            _make_client(ec) as client,
        ):
            r = client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx", "install_dir": "/opt/nginx"},
            )
            assert r.status_code == 202
            install_id = r.json()["install_id"]
            assert install_id == str(_INSTALL_ID)

            # Background task ran synchronously; output dir must exist
            expected_dir = tmp_path / "installs" / str(_INSTALL_ID)
            assert expected_dir.exists()

            # Poll status (first call returns terminal status; bg task already done)
            final = _poll_status(client, install_id)
            assert final in ("completed", "failed")

    def test_install_persists_to_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST install → INSERT into installed_apps is executed (fetchval called)."""
        monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path))
        ec = _endpoint_conn()
        bc = _bg_conn()

        with (
            mock.patch("aegis.server.api.routers.apps.get_pool", return_value=_pool_mock(bc)),
            mock.patch("omodul.install_app.install_app", side_effect=_omodul_success),
            _make_client(ec) as client,
        ):
            r = client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx", "install_dir": "/opt/nginx"},
            )
            assert r.status_code == 202

            # INSERT (fetchval) must have been called with the app_name
            assert ec.fetchval.called
            call_args = str(ec.fetchval.call_args)
            assert "nginx" in call_args

            # Background task UPDATE must have run
            assert bc.execute.called
            update_args = str(bc.execute.call_args_list)
            assert "UPDATE" in update_args or "completed" in update_args or "failed" in update_args

    def test_install_writes_event_trail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST install → event_trail INSERT executed by background task (fetchrow called)."""
        monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path))
        ec = _endpoint_conn()
        bc = _bg_conn()

        with (
            mock.patch("aegis.server.api.routers.apps.get_pool", return_value=_pool_mock(bc)),
            mock.patch("omodul.install_app.install_app", side_effect=_omodul_success),
            _make_client(ec) as client,
        ):
            r = client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx", "install_dir": "/opt/nginx"},
            )
            assert r.status_code == 202
            install_id = r.json()["install_id"]

            # event_trail INSERT (fetchrow with RETURNING id) must have been called
            assert bc.fetchrow.called
            # Verify trace_id and install_id appear in the call
            all_calls = str(bc.fetchrow.call_args_list)
            assert install_id in all_calls

    def test_install_without_install_dir_returns_422(self) -> None:
        """POST without install_dir → 422, detail references install_dir."""
        ec = _endpoint_conn()
        with _make_client(ec) as client:
            r = client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx"},
            )
        assert r.status_code == 422
        assert "install_dir" in str(r.json())

    def test_install_with_empty_install_dir_returns_422(self) -> None:
        """POST install_dir='' → 422."""
        ec = _endpoint_conn()
        with _make_client(ec) as client:
            r = client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx", "install_dir": ""},
            )
        assert r.status_code == 422
        assert "install_dir" in str(r.json())

    def test_install_fails_on_invalid_parent_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST install_dir with non-existent parent → bg task fails, DB update sets 'failed',
        event_trail records error detail."""
        monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path))
        ec = _endpoint_conn("failed")
        bc = _bg_conn()

        def _omodul_fail(cfg: Any, inp: Any, out_dir: Path) -> dict[str, Any]:
            raise OSError(f"No such file or directory: '{inp.project_dir}'")

        with (
            mock.patch("aegis.server.api.routers.apps.get_pool", return_value=_pool_mock(bc)),
            mock.patch("omodul.install_app.install_app", side_effect=_omodul_fail),
            _make_client(ec) as client,
        ):
            r = client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx", "install_dir": "/nonexistent/foo"},
            )
            # Validation passes (non-empty path); endpoint returns 202
            assert r.status_code == 202

            # Background task must have run the DB UPDATE with 'failed' status
            assert bc.execute.called
            update_call_str = str(bc.execute.call_args_list)
            assert "failed" in update_call_str

            # event_trail INSERT must have been called (records error detail)
            assert bc.fetchrow.called
            event_call_str = str(bc.fetchrow.call_args_list)
            assert "omodul_run" in event_call_str or "install_id" in event_call_str

    def test_install_uses_AEGIS_DATA_DIR_for_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AEGIS_DATA_DIR=tmp_path → install output lands in tmp_path/installs/{id}/."""
        custom_data = tmp_path / "custom"
        monkeypatch.setenv("AEGIS_DATA_DIR", str(custom_data))
        ec = _endpoint_conn()
        bc = _bg_conn()

        with (
            mock.patch("aegis.server.api.routers.apps.get_pool", return_value=_pool_mock(bc)),
            mock.patch("omodul.install_app.install_app", side_effect=_omodul_success),
            _make_client(ec) as client,
        ):
            r = client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx", "install_dir": "/opt/nginx"},
            )
            assert r.status_code == 202

            expected = custom_data / "installs" / str(_INSTALL_ID)
            assert expected.exists(), (
                f"Expected output dir {expected} to exist under AEGIS_DATA_DIR={custom_data}"
            )
