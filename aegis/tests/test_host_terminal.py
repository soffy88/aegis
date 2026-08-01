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
from aegis.server.api.routers.docker import _ws_authorize
from aegis.server.models import Role
from aegis.server.runtime.config import get_settings

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _token(role: str, org_id: uuid.UUID = _ORG, token_type: str = "access") -> str:
    from obase.auth import jwt_sign_hs256

    return jwt_sign_hs256(
        payload={
            "sub": str(_USER),
            "email": "t@example.com",
            "orgs": [{"org_id": str(org_id), "slug": "o", "role": role}],
            "type": token_type,
        },
        secret=get_settings().jwt_secret,
        expires_in_seconds=600,
    )


class TestAuthorizeHelper:
    def test_owner_ok(self) -> None:
        assert _ws_authorize(_token("owner"), _ORG, Role.OWNER) is True

    def test_admin_rejected_for_owner_gate(self) -> None:
        assert _ws_authorize(_token("admin"), _ORG, Role.OWNER) is False

    def test_admin_ok_for_admin_gate(self) -> None:
        assert _ws_authorize(_token("admin"), _ORG, Role.ADMIN) is True

    def test_wrong_org_rejected(self) -> None:
        assert _ws_authorize(_token("owner", org_id=_OTHER_ORG), _ORG, Role.OWNER) is False

    def test_garbage_token_rejected(self) -> None:
        assert _ws_authorize("not.a.jwt", _ORG, Role.OWNER) is False

    def test_non_access_token_rejected(self) -> None:
        assert _ws_authorize(_token("owner", token_type="refresh"), _ORG, Role.OWNER) is False


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
