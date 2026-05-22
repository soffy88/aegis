"""Tests for Bug #5 (data_dir paths) and Bug #6 (install_dir required).

Spec: BATCH 13
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import apps as apps_router
from aegis.server.app import create_app
from aegis.server.runtime.config import AegisSettings

_APP_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _apps_only_fa() -> FastAPI:
    fa: FastAPI = FastAPI()
    fa.include_router(apps_router.router)
    return fa


@pytest.fixture
def apps_conn() -> mock.AsyncMock:
    m: mock.AsyncMock = mock.AsyncMock()
    m.fetch.return_value = []
    m.fetchrow.return_value = None
    m.fetchval.return_value = _APP_ID
    m.execute.return_value = "DELETE 1"
    return m


@pytest.fixture
def apps_client(apps_conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
    fa = _apps_only_fa()

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield apps_conn

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Bug #5: data_dir lazy default + env override + startup mkdir
# ---------------------------------------------------------------------------


class TestDataDir:
    def test_install_uses_data_dir_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """data_dir defaults to ~/.aegis when AEGIS_DATA_DIR is not set."""
        monkeypatch.delenv("AEGIS_DATA_DIR", raising=False)
        s = AegisSettings()
        assert s.data_dir == Path.home() / ".aegis"

    def test_install_respects_AEGIS_DATA_DIR_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AEGIS_DATA_DIR env var overrides the default data_dir."""
        monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path))
        s = AegisSettings()
        assert s.data_dir == tmp_path

    def test_install_creates_data_dir_if_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """App startup lifespan creates data_dir when it does not exist."""
        data_dir = tmp_path / "aegis_data"
        monkeypatch.setenv("AEGIS_DATA_DIR", str(data_dir))
        assert not data_dir.exists()

        cfg = AegisSettings()

        mock_conn: mock.AsyncMock = mock.AsyncMock()
        mock_conn.fetch.return_value = []

        mock_pool_obj: mock.MagicMock = mock.MagicMock()
        mock_pool_obj.acquire.return_value.__aenter__ = mock.AsyncMock(
            return_value=mock_conn
        )
        mock_pool_obj.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)

        with mock.patch("aegis.server.app.init_pool", new_callable=mock.AsyncMock), \
             mock.patch("aegis.server.app.close_pool", new_callable=mock.AsyncMock), \
             mock.patch("aegis.server.app.get_pool", return_value=mock_pool_obj), \
             mock.patch(
                 "aegis.server.app.apply_migrations",
                 new_callable=mock.AsyncMock,
                 return_value=0,
             ):
            app = create_app(settings=cfg)
            with TestClient(app):
                assert data_dir.exists()


# ---------------------------------------------------------------------------
# Bug #6: install_dir required — endpoint returns 422 when invalid
# ---------------------------------------------------------------------------


class TestInstallDirRequired:
    def test_install_dir_missing_returns_422(
        self, apps_client: TestClient
    ) -> None:
        """POST /install without install_dir → 422, detail references install_dir."""
        with mock.patch("aegis.server.api.routers.apps._run_install"):
            r = apps_client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx"},
            )
        assert r.status_code == 422
        assert "install_dir" in str(r.json())

    def test_install_dir_empty_string_returns_422(
        self, apps_client: TestClient
    ) -> None:
        """POST /install with install_dir="" → 422."""
        with mock.patch("aegis.server.api.routers.apps._run_install"):
            r = apps_client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx", "install_dir": ""},
            )
        assert r.status_code == 422
        assert "install_dir" in str(r.json())

    def test_install_dir_whitespace_only_returns_422(
        self, apps_client: TestClient
    ) -> None:
        """POST /install with install_dir='   ' → 422 (whitespace stripped, then rejected)."""
        with mock.patch("aegis.server.api.routers.apps._run_install"):
            r = apps_client.post(
                "/api/v1/apps/install",
                json={"app_name": "nginx", "install_dir": "   "},
            )
        assert r.status_code == 422
        assert "install_dir" in str(r.json())
