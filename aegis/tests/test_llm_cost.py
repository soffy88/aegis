"""Tests for the LLM cost ledger (record + per-org spend + API)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import brain as brain_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.services.llm_cost import org_spend, record_cost

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _pool_yielding(conn: mock.AsyncMock) -> mock.MagicMock:
    pool = mock.MagicMock()
    pool.acquire.return_value.__aenter__ = mock.AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = mock.AsyncMock(return_value=False)
    return pool


# ── record ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_cost_inserts() -> None:
    conn = mock.AsyncMock()
    with mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)):
        await record_cost(principal=str(_ORG), omodul_name="rca", model="sonnet", cost_usd=0.42)
    assert "INSERT INTO llm_cost_ledger" in conn.execute.await_args.args[0]
    assert conn.execute.await_args.args[4] == 0.42


@pytest.mark.asyncio
async def test_record_cost_skips_zero() -> None:
    conn = mock.AsyncMock()
    with mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)):
        await record_cost(principal="p", omodul_name="x", model="m", cost_usd=0.0)
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_record_cost_swallows_errors() -> None:
    conn = mock.AsyncMock()
    conn.execute.side_effect = RuntimeError("db down")
    with mock.patch("aegis.server.persistence.db.get_pool", return_value=_pool_yielding(conn)):
        await record_cost(principal="p", omodul_name="x", model="m", cost_usd=1.0)  # no raise


# ── aggregation ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_org_spend_aggregates() -> None:
    conn = mock.AsyncMock()
    conn.fetchval.return_value = 1.23
    conn.fetch.return_value = [
        {"model": "sonnet", "usd": 1.0, "calls": 3},
        {"model": "haiku", "usd": 0.23, "calls": 5},
    ]
    out = await org_spend(conn, org_id=_ORG, days=7)
    assert out["total_usd"] == 1.23
    assert out["by_model"][0] == {"model": "sonnet", "usd": 1.0, "calls": 3}


# ── API ──────────────────────────────────────────────────────────────────────────


def test_spend_endpoint() -> None:
    conn = mock.AsyncMock()
    conn.fetchval.return_value = 2.5
    conn.fetch.return_value = []
    app = FastAPI()
    app.include_router(brain_router.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=uuid.uuid4(), email="a@x.com", orgs=[OrgInToken(org_id=_ORG, slug="o", role="viewer")]
    )

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    r = TestClient(app, raise_server_exceptions=False).get(
        f"/api/v1/orgs/{_ORG}/brain/spend?days=14"
    )
    assert r.status_code == 200
    assert r.json()["total_usd"] == 2.5
