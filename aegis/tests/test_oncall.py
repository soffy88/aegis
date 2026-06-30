"""Tests for on-call rotation + API."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import oncall as oncall_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.services.oncall import current_oncall, whose_shift

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_U1, _U2, _U3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
_ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)
_DAY = 86400


# ── rotation math ────────────────────────────────────────────────────────────────


def test_whose_shift_rotates_by_shift_length() -> None:
    rot = [_U1, _U2, _U3]
    assert whose_shift(rot, shift_length_seconds=_DAY, anchor_at=_ANCHOR, now=_ANCHOR) == _U1
    assert whose_shift(rot, shift_length_seconds=_DAY, anchor_at=_ANCHOR,
                       now=_ANCHOR + timedelta(days=1)) == _U2
    assert whose_shift(rot, shift_length_seconds=_DAY, anchor_at=_ANCHOR,
                       now=_ANCHOR + timedelta(days=3)) == _U1  # wraps around
    # mid-shift stays on the same person
    assert whose_shift(rot, shift_length_seconds=_DAY, anchor_at=_ANCHOR,
                       now=_ANCHOR + timedelta(hours=23)) == _U1


def test_whose_shift_empty_rotation_returns_none() -> None:
    assert whose_shift([], shift_length_seconds=_DAY, anchor_at=_ANCHOR, now=_ANCHOR) is None


@pytest.mark.asyncio
async def test_current_oncall_none_when_no_schedule() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = None
    assert await current_oncall(conn, org_id=_ORG) is None


@pytest.mark.asyncio
async def test_current_oncall_uses_schedule() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {
        "rotation": [_U1, _U2], "shift_length_seconds": _DAY, "anchor_at": _ANCHOR,
    }
    out = await current_oncall(conn, org_id=_ORG, now=_ANCHOR + timedelta(days=1))
    assert out == _U2


# ── API ──────────────────────────────────────────────────────────────────────────


def _client(conn: mock.AsyncMock, role: str) -> TestClient:
    app = FastAPI()
    app.include_router(oncall_router.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=uuid.uuid4(), email="a@x.com", orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)]
    )

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def test_create_schedule_admin_only() -> None:
    conn = mock.AsyncMock()
    r = _client(conn, "member").post(
        f"/api/v1/orgs/{_ORG}/oncall/schedules",
        json={"name": "primary", "rotation": [str(_U1)]},
    )
    assert r.status_code == 403  # MODIFY_ORG is admin+


def test_create_schedule_ok() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {
        "id": uuid.uuid4(), "org_id": _ORG, "name": "primary", "rotation": [_U1],
        "shift_length_seconds": _DAY, "anchor_at": _ANCHOR, "enabled": True,
        "created_at": _ANCHOR,
    }
    r = _client(conn, "admin").post(
        f"/api/v1/orgs/{_ORG}/oncall/schedules",
        json={"name": "primary", "rotation": [str(_U1)], "shift_length_seconds": _DAY},
    )
    assert r.status_code == 201
    assert r.json()["name"] == "primary"


def test_current_endpoint(conn_user: str = "viewer") -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {
        "rotation": [_U1], "shift_length_seconds": _DAY, "anchor_at": _ANCHOR,
    }
    r = _client(conn, "viewer").get(f"/api/v1/orgs/{_ORG}/oncall/current")
    assert r.status_code == 200
    assert r.json()["oncall_user_id"] == str(_U1)
