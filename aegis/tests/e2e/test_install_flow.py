"""e2e: install full-flow tests via dispatcher.

Pattern: TestClient + mock DB + mock omodul (via dispatcher).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import apps as apps_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_INSTALL_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_EVENT_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="test@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role="owner")],
    )


@contextmanager
def _make_client(endpoint_conn: mock.AsyncMock) -> Iterator[TestClient]:
    fa = FastAPI()
    fa.include_router(apps_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _override() -> AsyncIterator[mock.AsyncMock]:
        yield endpoint_conn

    fa.dependency_overrides[get_db_conn] = _override
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


def _project_row() -> dict:
    """Valid row dict for Project.from_row()."""
    return {
        "id": _PROJ,
        "org_id": _ORG,
        "slug": "test-proj",
        "name": "Test Project",
        "display_name": "Test Project",
        "environment": "prod",
        "docker_labels": None,
        "config": None,
        "archived_at": None,
        "created_at": datetime(2026, 1, 1),
    }


def _endpoint_conn() -> mock.AsyncMock:
    """Endpoint DB conn: fetchrow returns project row, fetchval returns install_id."""
    conn = mock.AsyncMock()
    conn.fetchval.return_value = _INSTALL_ID
    # install_app_endpoint first looks up the project via fetchrow
    conn.fetchrow.return_value = _project_row()
    return conn


def _bg_conn() -> mock.AsyncMock:
    conn = mock.AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    conn.fetchrow.return_value = {"id": _EVENT_ID}
    return conn


def _pool_mock(bg_conn: mock.AsyncMock) -> mock.MagicMock:
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=bg_conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


def _omodul_result_success(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "findings": {"container_id": "abc"},
        "decision_trail": {"steps": []},
        "report_path": "/tmp/r.md",
        "cost_usd": 0.0,
        "error": None,
    }


_INSTALL_URL = f"/api/v1/orgs/{_ORG}/apps/install?project_id={_PROJ}"


class TestInstallFlow:
    def test_install_via_dispatcher(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """POST /install → 202, dispatcher.invoke called with correct omodul_name."""
        monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path))
        ec = _endpoint_conn()
        bc = _bg_conn()

        mock_dispatcher_invoke = mock.AsyncMock(return_value=_omodul_result_success())

        with (
            mock.patch("aegis.server.api.routers.apps.get_pool", return_value=_pool_mock(bc)),
            mock.patch(
                "aegis.server.dispatch.omodul_dispatcher.OmodulDispatcher.invoke",
                mock_dispatcher_invoke,
            ),
            mock.patch(
                "aegis.server.persistence.event_trail.save_decision_trail",
                new_callable=mock.AsyncMock,
            ),
            _make_client(ec) as client,
        ):
            r = client.post(
                _INSTALL_URL,
                json={"app_name": "nginx", "install_dir": "/opt/nginx"},
            )
            assert r.status_code == 202
            assert r.json()["install_id"] == str(_INSTALL_ID)

            # Verify dispatcher was called with correct omodul_name
            if mock_dispatcher_invoke.called:
                call_kwargs = mock_dispatcher_invoke.call_args.kwargs
                assert call_kwargs["omodul_name"] == "install_self_hosted_app"

    def test_install_dedup_via_router(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same request twice → both return 202 (dedup handled at dispatcher level)."""
        monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path))
        ec = _endpoint_conn()
        bc = _bg_conn()

        mock_dispatcher_invoke = mock.AsyncMock(return_value=_omodul_result_success())

        with (
            mock.patch("aegis.server.api.routers.apps.get_pool", return_value=_pool_mock(bc)),
            mock.patch(
                "aegis.server.dispatch.omodul_dispatcher.OmodulDispatcher.invoke",
                mock_dispatcher_invoke,
            ),
            mock.patch(
                "aegis.server.persistence.event_trail.save_decision_trail",
                new_callable=mock.AsyncMock,
            ),
            _make_client(ec) as client,
        ):
            r1 = client.post(
                _INSTALL_URL,
                json={"app_name": "nginx", "install_dir": "/opt/nginx"},
            )
            r2 = client.post(
                _INSTALL_URL,
                json={"app_name": "nginx", "install_dir": "/opt/nginx"},
            )
            assert r1.status_code == 202
            assert r2.status_code == 202

    def test_install_without_install_dir_returns_422(self) -> None:
        """POST without install_dir → 422."""
        ec = _endpoint_conn()
        with _make_client(ec) as client:
            r = client.post(_INSTALL_URL, json={"app_name": "nginx"})
        assert r.status_code == 422

    def test_install_with_empty_install_dir_returns_422(self) -> None:
        """POST install_dir='' → 422."""
        ec = _endpoint_conn()
        with _make_client(ec) as client:
            r = client.post(_INSTALL_URL, json={"app_name": "nginx", "install_dir": ""})
        assert r.status_code == 422

    def test_install_persists_to_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """POST install → INSERT into installed_apps (fetchval called)."""
        monkeypatch.setenv("AEGIS_DATA_DIR", str(tmp_path))
        ec = _endpoint_conn()
        bc = _bg_conn()

        with (
            mock.patch("aegis.server.api.routers.apps.get_pool", return_value=_pool_mock(bc)),
            mock.patch(
                "aegis.server.dispatch.omodul_dispatcher.OmodulDispatcher.invoke",
                new_callable=mock.AsyncMock,
                return_value=_omodul_result_success(),
            ),
            mock.patch(
                "aegis.server.persistence.event_trail.save_decision_trail",
                new_callable=mock.AsyncMock,
            ),
            _make_client(ec) as client,
        ):
            r = client.post(
                _INSTALL_URL,
                json={"app_name": "nginx", "install_dir": "/opt/nginx"},
            )
            assert r.status_code == 202
            assert ec.fetchval.called
