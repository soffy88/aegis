"""Tests for DDNS remote-access service + router."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import UTC
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import remote_access as ra_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.services import ddns as ddns_svc
from aegis.server.services.ddns import DdnsPrimitiveUnavailable

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_CFG = uuid.UUID("33333333-3333-3333-3333-333333333333")


class FakeConn:
    def __init__(
        self, *, row: dict[str, Any] | None = None, execute_result: str = "DELETE 1"
    ) -> None:
        self.row = row
        self.rows: list[dict[str, Any]] = []
        self.execute_result = execute_result
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, _q: str, *args: Any) -> dict[str, Any] | None:
        return self.row

    async def fetch(self, _q: str, *args: Any) -> list[dict[str, Any]]:
        return self.rows

    async def fetchval(self, _q: str, *args: Any) -> Any:
        return None

    async def execute(self, q: str, *args: Any) -> str:
        self.executed.append((q, args))
        return self.execute_result


def _cfg_row(**over: Any) -> dict[str, Any]:
    from datetime import datetime

    base = {
        "id": _CFG,
        "provider": "duckdns",
        "hostname": "myhost.duckdns.org",
        "username": None,
        "base_url": None,
        "enabled": True,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _stub_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _store(conn: Any, **_k: Any) -> dict[str, Any]:
        return {"name": "x", "version": 1}

    async def _reveal(conn: Any, **_k: Any) -> str:
        return "s3cr3t"

    monkeypatch.setattr(ddns_svc.secrets_vault, "store_secret", _store)
    monkeypatch.setattr(ddns_svc.secrets_vault, "reveal_secret", _reveal)


# ── service ──────────────────────────────────────────────────────────────────


async def test_create_rejects_bad_provider() -> None:
    with pytest.raises(ValueError, match="unsupported provider"):
        await ddns_svc.create_config(
            FakeConn(), org_id=_ORG, provider="route53", hostname="h", secret="s"
        )


async def test_create_dyndns2_requires_username() -> None:
    with pytest.raises(ValueError, match="username"):
        await ddns_svc.create_config(
            FakeConn(), org_id=_ORG, provider="dyndns2", hostname="h", secret="s"
        )


async def test_create_stores_secret_and_returns_public() -> None:
    out = await ddns_svc.create_config(
        FakeConn(row=_cfg_row()),
        org_id=_ORG,
        provider="duckdns",
        hostname="myhost.duckdns.org",
        secret="tok",
    )
    assert out["provider"] == "duckdns"
    assert "secret" not in out  # credential never returned
    assert "token" not in out


async def test_update_now_unavailable_without_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real venv oprim has no ddns_update yet → typed unavailable error.
    def boom() -> Any:
        raise DdnsPrimitiveUnavailable("needs oprim>=3.21")

    monkeypatch.setattr(ddns_svc, "_ddns_update", boom)
    with pytest.raises(DdnsPrimitiveUnavailable):
        await ddns_svc.update_now(FakeConn(row=_cfg_row()), org_id=_ORG, config_id=_CFG)


async def test_update_now_records_result(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_update(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            status="ok", ip="1.2.3.4", success=True, changed=True, raw="good 1.2.3.4"
        )

    monkeypatch.setattr(ddns_svc, "_ddns_update", lambda: fake_update)
    conn = FakeConn(row=_cfg_row())
    out = await ddns_svc.update_now(conn, org_id=_ORG, config_id=_CFG)
    assert out == {"success": True, "changed": True, "status": "ok", "ip": "1.2.3.4"}
    assert captured["provider"] == "duckdns"
    assert captured["token"] == "s3cr3t"
    assert any("UPDATE ddns_configs" in q for q, _ in conn.executed)


async def test_update_now_config_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ddns_svc, "_ddns_update", lambda: lambda **k: None)
    with pytest.raises(LookupError, match="not found"):
        await ddns_svc.update_now(FakeConn(row=None), org_id=_ORG, config_id=_CFG)


# ── router ───────────────────────────────────────────────────────────────────


async def _fake_user() -> UserContext:
    return UserContext(
        user_id=_USER,
        email="t@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="org", role="owner")],
    )


@pytest.fixture
def client() -> Generator[tuple[TestClient, FakeConn], None, None]:
    conn = FakeConn(row=_cfg_row())
    fa = FastAPI()
    fa.include_router(ra_router.router)
    fa.dependency_overrides[get_current_user] = _fake_user

    async def _conn() -> AsyncIterator[FakeConn]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _conn
    with TestClient(fa, raise_server_exceptions=False) as c:
        yield c, conn


def test_router_create_ddns(client: tuple[TestClient, FakeConn]) -> None:
    c, _ = client
    r = c.post(
        f"/api/v1/orgs/{_ORG}/remote-access/ddns",
        json={"provider": "duckdns", "hostname": "myhost.duckdns.org", "secret": "tok"},
    )
    assert r.status_code == 201
    assert r.json()["provider"] == "duckdns"


def test_router_update_503_when_primitive_absent(client: tuple[TestClient, FakeConn]) -> None:
    # Real venv oprim lacks ddns_update → 503 (not a crash).
    c, _ = client
    r = c.post(f"/api/v1/orgs/{_ORG}/remote-access/ddns/{_CFG}/update")
    assert r.status_code == 503


def test_router_delete_404_when_absent(client: tuple[TestClient, FakeConn]) -> None:
    c, conn = client
    conn.execute_result = "DELETE 0"
    r = c.delete(f"/api/v1/orgs/{_ORG}/remote-access/ddns/{_CFG}")
    assert r.status_code == 404
