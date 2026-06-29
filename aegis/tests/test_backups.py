"""Tests for backups router — endpoints + background task success/failure paths."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import backups as backups_router
from aegis.server.api.routers.backups import (
    BackupRequest,
    RestoreRequest,
    _run_backup,
    _run_restore,
)
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_BACKUP = uuid.UUID("33333333-3333-3333-3333-333333333333")


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="t@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="org", role="owner")],
    )


def _backup_row(status: str = "completed") -> dict:
    return {
        "id": _BACKUP,
        "org_id": _ORG,
        "app_slug": "nextcloud",
        "instance_name": "nc1",
        "status": status,
        "backup_key": "s3://b/k",
        "size_bytes": 123,
        "error": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "completed_at": None,
    }


@pytest.fixture
def conn() -> mock.AsyncMock:
    return mock.AsyncMock()


@pytest.fixture
def client(conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
    fa = FastAPI()
    fa.include_router(backups_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c


# ── endpoints ──────────────────────────────────────────────────────────────────


def test_create_backup_returns_pending(client: TestClient, conn: mock.AsyncMock) -> None:
    conn.fetchval.return_value = _BACKUP
    r = client.post(
        f"/api/v1/orgs/{_ORG}/backups",
        json={"app_slug": "nextcloud", "instance_name": "nc1", "target_volume": "v"},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "pending"


def test_get_backup_404_when_missing(client: TestClient, conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = None
    r = client.get(f"/api/v1/orgs/{_ORG}/backups/{_BACKUP}")
    assert r.status_code == 404


def test_restore_rejects_incomplete_backup(client: TestClient, conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = _backup_row(status="pending")
    r = client.post(
        f"/api/v1/orgs/{_ORG}/backups/{_BACKUP}/restore",
        json={"target_volume": "v"},
    )
    assert r.status_code == 400


def test_restore_accepts_completed_backup(client: TestClient, conn: mock.AsyncMock) -> None:
    conn.fetchrow.return_value = _backup_row(status="completed")
    r = client.post(
        f"/api/v1/orgs/{_ORG}/backups/{_BACKUP}/restore",
        json={"target_volume": "v"},
    )
    assert r.status_code == 202


def test_delete_404_when_no_row(client: TestClient, conn: mock.AsyncMock) -> None:
    conn.execute.return_value = "DELETE 0"
    r = client.delete(f"/api/v1/orgs/{_ORG}/backups/{_BACKUP}")
    assert r.status_code == 404


# ── background tasks ───────────────────────────────────────────────────────────


def _pool_yielding(conn: mock.AsyncMock) -> mock.MagicMock:
    pool_cm = mock.MagicMock()
    pool_cm.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool_cm.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool_cm


@pytest.mark.asyncio
async def test_run_backup_marks_completed_on_success() -> None:
    conn = mock.AsyncMock()
    fake_backup = mock.MagicMock(return_value={"storage_url": "s3://b/k", "total_size_bytes": 42})
    with (
        mock.patch("aegis.server.api.routers.backups.get_pool", return_value=_pool_yielding(conn)),
        mock.patch("omodul.backup_app_data.backup_app_data", fake_backup),
    ):
        await _run_backup(
            _BACKUP, _ORG, BackupRequest(app_slug="a", instance_name="i", target_volume="v")
        )

    sql = " ".join(call.args[0] for call in conn.execute.await_args_list)
    assert "completed" in sql


@pytest.mark.asyncio
async def test_run_restore_marks_failed_on_error() -> None:
    conn = mock.AsyncMock()

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("restore blew up")

    with (
        mock.patch("aegis.server.api.routers.backups.get_pool", return_value=_pool_yielding(conn)),
        mock.patch("oskill.restore_from_backup.restore_from_backup", _boom),
    ):
        await _run_restore(_BACKUP, _ORG, RestoreRequest(target_volume="v"), _backup_row())

    sql = " ".join(call.args[0] for call in conn.execute.await_args_list)
    assert "failed" in sql
