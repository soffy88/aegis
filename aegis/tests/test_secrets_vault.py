"""Tests for the encrypted secrets vault."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import secrets as secrets_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user
from aegis.server.runtime.config import AegisSettings
from aegis.server.services.secrets_vault import master_key, reveal_secret, store_secret

_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _cfg(**kw) -> AegisSettings:
    return AegisSettings(**kw)  # type: ignore[arg-type]


# ── crypto / service ─────────────────────────────────────────────────────────────


def test_master_key_is_32_bytes_derived() -> None:
    assert len(master_key(_cfg())) == 32


def test_master_key_uses_explicit_hex_when_set() -> None:
    key_hex = "ab" * 32
    assert master_key(_cfg(secrets_master_key=key_hex)) == bytes.fromhex(key_hex)


@pytest.mark.asyncio
async def test_store_encrypts_and_returns_metadata_only() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {
        "name": "db_pw", "version": 1, "created_at": _NOW, "rotated_at": _NOW,
    }
    meta = await store_secret(conn, org_id=_ORG, name="db_pw", value="hunter2", cfg=_cfg())
    # ciphertext passed to INSERT must NOT equal the plaintext
    ciphertext_arg = conn.fetchrow.await_args.args[3]
    assert ciphertext_arg != "hunter2"
    assert "value" not in meta and meta["version"] == 1


@pytest.mark.asyncio
async def test_reveal_round_trips_real_ciphertext() -> None:
    cfg = _cfg()
    from obase import encrypt_token

    ct = encrypt_token(plaintext="s3cr3t", master_key=master_key(cfg))
    conn = mock.AsyncMock()
    conn.fetchval.return_value = ct
    assert await reveal_secret(conn, org_id=_ORG, name="x", cfg=cfg) == "s3cr3t"


@pytest.mark.asyncio
async def test_reveal_none_when_absent() -> None:
    conn = mock.AsyncMock()
    conn.fetchval.return_value = None
    assert await reveal_secret(conn, org_id=_ORG, name="x", cfg=_cfg()) is None


# ── API ──────────────────────────────────────────────────────────────────────────


def _client(conn: mock.AsyncMock, role: str) -> TestClient:
    app = FastAPI()
    app.include_router(secrets_router.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=uuid.uuid4(), email="a@x.com", orgs=[OrgInToken(org_id=_ORG, slug="o", role=role)]
    )

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    app.dependency_overrides[get_db_conn] = _conn
    return TestClient(app, raise_server_exceptions=False)


def test_put_secret_admin_only() -> None:
    conn = mock.AsyncMock()
    r = _client(conn, "member").post(
        f"/api/v1/orgs/{_ORG}/secrets", json={"name": "k", "value": "v"}
    )
    assert r.status_code == 403  # MODIFY_ORG is admin+


def test_put_and_response_has_no_value() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = {
        "name": "k", "version": 1, "created_at": _NOW, "rotated_at": _NOW,
    }
    r = _client(conn, "admin").post(
        f"/api/v1/orgs/{_ORG}/secrets", json={"name": "k", "value": "topsecret"}
    )
    assert r.status_code == 201
    assert "value" not in r.json() and "ciphertext" not in r.json()


def test_put_rejects_bad_name() -> None:
    conn = mock.AsyncMock()
    r = _client(conn, "admin").post(
        f"/api/v1/orgs/{_ORG}/secrets", json={"name": "bad name!", "value": "v"}
    )
    assert r.status_code == 422


def test_rotate_404_when_absent() -> None:
    conn = mock.AsyncMock()
    conn.fetchrow.return_value = None
    r = _client(conn, "admin").post(
        f"/api/v1/orgs/{_ORG}/secrets/k/rotate", json={"value": "new"}
    )
    assert r.status_code == 404
