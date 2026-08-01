"""Tests for the host-terminal WebSocket authorization (owner-only break-glass).

The PTY bridge itself needs a real Docker daemon + privileged helper, so these
tests cover the security gate: token validity, org membership, and owner-only
role enforcement. The reject path closes before any Docker interaction.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aegis.server.api.routers import docker as docker_router
from aegis.server.api.routers.docker import _ws_user
from aegis.server.models import Role
from aegis.server.runtime.config import get_settings

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _token(
    role: str,
    org_id: uuid.UUID = _ORG,
    token_type: str = "access",
    epoch: int = 7,
) -> str:
    from obase.auth import jwt_sign_hs256

    return jwt_sign_hs256(
        payload={
            "sub": str(_USER),
            "email": "t@example.com",
            "orgs": [{"org_id": str(org_id), "slug": "o", "role": role}],
            "type": token_type,
            "epoch": epoch,
        },
        secret=get_settings().jwt_secret,
        expires_in_seconds=600,
    )


class _Conn:
    def __init__(self, epoch: int | None) -> None:
        self.epoch = epoch

    async def fetchval(self, *_args: object) -> int | None:
        return self.epoch


class _Acquire:
    def __init__(self, epoch: int | None) -> None:
        self.conn = _Conn(epoch)

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Pool:
    def __init__(self, epoch: int | None) -> None:
        self.epoch = epoch

    def acquire(self) -> _Acquire:
        return _Acquire(self.epoch)


def _patch_pool(monkeypatch: pytest.MonkeyPatch, epoch: int | None = 7) -> None:
    monkeypatch.setattr(docker_router, "get_pool", lambda: _Pool(epoch))


class TestAuthorizeHelper:
    @pytest.mark.asyncio
    async def test_owner_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pool(monkeypatch)
        assert await _ws_user(_token("owner"), _ORG, Role.OWNER) is not None

    @pytest.mark.asyncio
    async def test_admin_rejected_for_owner_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pool(monkeypatch)
        assert await _ws_user(_token("admin"), _ORG, Role.OWNER) is None

    @pytest.mark.asyncio
    async def test_admin_ok_for_admin_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pool(monkeypatch)
        assert await _ws_user(_token("admin"), _ORG, Role.ADMIN) is not None

    @pytest.mark.asyncio
    async def test_wrong_org_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pool(monkeypatch)
        assert await _ws_user(_token("owner", org_id=_OTHER_ORG), _ORG, Role.OWNER) is None

    @pytest.mark.asyncio
    async def test_garbage_token_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pool(monkeypatch)
        assert await _ws_user("not.a.jwt", _ORG, Role.OWNER) is None

    @pytest.mark.asyncio
    async def test_non_access_token_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pool(monkeypatch)
        assert await _ws_user(_token("owner", token_type="refresh"), _ORG, Role.OWNER) is None

    @pytest.mark.asyncio
    async def test_stale_epoch_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pool(monkeypatch, epoch=8)
        assert await _ws_user(_token("owner", epoch=7), _ORG, Role.OWNER) is None

    @pytest.mark.asyncio
    async def test_inactive_or_missing_user_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_pool(monkeypatch, epoch=None)
        assert await _ws_user(_token("owner"), _ORG, Role.OWNER) is None


@pytest.fixture
def client() -> TestClient:
    fa = FastAPI()
    fa.include_router(docker_router.router)
    return TestClient(fa, raise_server_exceptions=False)


class TestHostTerminalEndpoint:
    def test_rejects_non_owner_before_docker(self, client: TestClient) -> None:
        url = f"/api/v1/orgs/{_ORG}/docker/host-terminal?token={_token('admin')}"
        # closed 1008 before accept
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url):
            pass

    def test_rejects_garbage_token(self, client: TestClient) -> None:
        url = f"/api/v1/orgs/{_ORG}/docker/host-terminal?token=bad"
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(url):
            pass
