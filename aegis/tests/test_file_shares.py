"""Tests for file-manager share links (service + public download route)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import file_share_public as public_router
from aegis.server.services import file_shares as sharesvc
from aegis.server.services.file_shares import ShareNotValid

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_SHARE = uuid.UUID("33333333-3333-3333-3333-333333333333")


class FakeConn:
    """Minimal asyncpg-conn stand-in with a working transaction() context manager."""

    def __init__(
        self, *, row: dict[str, Any] | None = None, execute_result: str = "UPDATE 1"
    ) -> None:
        self.row = row
        self.rows: list[dict[str, Any]] = []
        self.execute_result = execute_result
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, _q: str, *args: Any) -> dict[str, Any] | None:
        return self.row

    async def fetch(self, _q: str, *args: Any) -> list[dict[str, Any]]:
        return self.rows

    async def execute(self, q: str, *args: Any) -> str:
        self.executed.append((q, args))
        return self.execute_result

    def transaction(self) -> Any:
        class _Tx:
            async def __aenter__(self_inner) -> None:
                return None

            async def __aexit__(self_inner, *_a: Any) -> bool:
                return False

        return _Tx()


@pytest.fixture
def shared_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 hello")
    # Bypass the sandbox-root resolution; just return this real file.
    monkeypatch.setattr("aegis.server.services.files.resolve_for_download", lambda _p: f)
    return f


def _share_row(**over: Any) -> dict[str, Any]:
    base = {
        "id": _SHARE,
        "path": "/data/report.pdf",
        "filename": "report.pdf",
        "expires_at": None,
        "max_downloads": None,
        "download_count": 0,
        "revoked": False,
    }
    base.update(over)
    return base


# ── create_share ────────────────────────────────────────────────────────────


async def test_create_share_returns_one_time_token(shared_file: Path) -> None:
    conn = FakeConn(
        row={
            "id": _SHARE,
            "filename": "report.pdf",
            "expires_at": None,
            "max_downloads": None,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    out = await sharesvc.create_share(
        conn, org_id=_ORG, path="/data/report.pdf", created_by=_USER, expires_in_hours=24
    )
    assert out["filename"] == "report.pdf"
    assert len(out["token"]) >= 32  # high-entropy urlsafe token
    assert out["id"] == str(_SHARE)


async def test_create_share_rejects_disallowed_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(_p: str) -> Path:
        raise ValueError("path outside allowed roots")

    monkeypatch.setattr("aegis.server.services.files.resolve_for_download", deny)
    with pytest.raises(ValueError, match="outside allowed roots"):
        await sharesvc.create_share(FakeConn(), org_id=_ORG, path="/etc/passwd", created_by=_USER)


# ── resolve_share ───────────────────────────────────────────────────────────


async def test_resolve_share_happy_bumps_count(shared_file: Path) -> None:
    conn = FakeConn(row=_share_row())
    path, filename = await sharesvc.resolve_share(conn, token="tok")
    assert path == shared_file
    assert filename == "report.pdf"
    # download_count increment issued
    assert any("download_count = download_count + 1" in q for q, _ in conn.executed)


async def test_resolve_share_unknown_token(shared_file: Path) -> None:
    with pytest.raises(ShareNotValid, match="not found"):
        await sharesvc.resolve_share(FakeConn(row=None), token="nope")


async def test_resolve_share_revoked(shared_file: Path) -> None:
    with pytest.raises(ShareNotValid, match="revoked"):
        await sharesvc.resolve_share(FakeConn(row=_share_row(revoked=True)), token="tok")


async def test_resolve_share_expired(shared_file: Path) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(ShareNotValid, match="expired"):
        await sharesvc.resolve_share(FakeConn(row=_share_row(expires_at=past)), token="tok")


async def test_resolve_share_over_limit(shared_file: Path) -> None:
    row = _share_row(max_downloads=3, download_count=3)
    with pytest.raises(ShareNotValid, match="limit"):
        await sharesvc.resolve_share(FakeConn(row=row), token="tok")


async def test_resolve_share_file_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(_p: str) -> Path:
        raise FileNotFoundError("moved")

    monkeypatch.setattr("aegis.server.services.files.resolve_for_download", gone)
    with pytest.raises(ShareNotValid, match="no longer available"):
        await sharesvc.resolve_share(FakeConn(row=_share_row()), token="tok")


# ── revoke_share ────────────────────────────────────────────────────────────


async def test_revoke_share_true_when_updated() -> None:
    assert (
        await sharesvc.revoke_share(
            FakeConn(execute_result="UPDATE 1"), org_id=_ORG, share_id=_SHARE
        )
        is True
    )


async def test_revoke_share_false_when_no_row() -> None:
    assert (
        await sharesvc.revoke_share(
            FakeConn(execute_result="UPDATE 0"), org_id=_ORG, share_id=_SHARE
        )
        is False
    )


# ── public /s/{token} route ─────────────────────────────────────────────────


@pytest.fixture
def public_client(shared_file: Path) -> Generator[tuple[TestClient, FakeConn], None, None]:
    conn = FakeConn(row=_share_row())
    fa = FastAPI()
    fa.include_router(public_router.router)

    async def _conn() -> AsyncIterator[FakeConn]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c, conn


def test_public_download_streams_file(public_client: tuple[TestClient, FakeConn]) -> None:
    client, _ = public_client
    r = client.get("/s/sometoken")
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 hello"


def test_public_download_404_on_invalid(public_client: tuple[TestClient, FakeConn]) -> None:
    client, conn = public_client
    conn.row = None  # unknown token
    r = client.get("/s/bad")
    assert r.status_code == 404
