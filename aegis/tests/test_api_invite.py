"""Tests for invite flow — create, verify, accept."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aegis.server.api.deps import get_db_conn
from aegis.server.api.routers import invite as invite_router
from aegis.server.auth.dependencies import OrgInToken, UserContext, get_current_user

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_INVITE_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_TOKEN = "abc123def456" * 5  # 60-char hex-like token


def _user_with_role(role: str) -> UserContext:
    return UserContext(
        user_id=_USER,
        email="admin@example.com",
        orgs=[OrgInToken(org_id=_ORG, slug="test-org", role=role)],
    )


def _invite_row(
    *,
    token: str = _TOKEN,
    email: str = "new@example.com",
    role: str = "member",
    accepted_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict:
    return {
        "id": _INVITE_ID,
        "token": token,
        "org_id": _ORG,
        "email": email,
        "role": role,
        "invited_by": _USER,
        "expires_at": expires_at or (datetime.now(UTC) + timedelta(days=7)),
        "accepted_at": accepted_at,
        "created_at": datetime.now(UTC),
        "org_name": "Test Org",
    }


@pytest.fixture
def conn() -> mock.AsyncMock:
    m = mock.AsyncMock()
    m.fetchrow.return_value = _invite_row()
    m.fetchval.return_value = _INVITE_ID
    m.execute.return_value = "UPDATE 1"
    return m


def _make_app(role: str, conn: mock.AsyncMock) -> FastAPI:
    fa = FastAPI()
    fa.include_router(invite_router.router)
    u = _user_with_role(role)
    fa.dependency_overrides[get_current_user] = lambda: u

    async def _conn() -> AsyncIterator[mock.AsyncMock]:
        yield conn

    fa.dependency_overrides[get_db_conn] = _conn
    return fa


@pytest.fixture
def admin_client(conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
    with TestClient(_make_app("admin", conn), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def viewer_client(conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
    with TestClient(_make_app("viewer", conn), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def member_client(conn: mock.AsyncMock) -> Generator[TestClient, None, None]:
    with TestClient(_make_app("member", conn), raise_server_exceptions=False) as c:
        yield c


# ── create invite ────────────────────────────────────────────────────────────


class TestCreateInvite:
    def test_admin_can_create_invite(self, admin_client: TestClient) -> None:
        with mock.patch("aegis.server.api.routers.invite._send_invite_email"):
            r = admin_client.post(
                f"/api/v1/orgs/{_ORG}/invites",
                json={"email": "new@example.com", "role": "member"},
            )
        assert r.status_code == 201
        body = r.json()
        assert "token" in body
        assert body["email"] == "new@example.com"

    def test_viewer_cannot_create_invite(self, viewer_client: TestClient) -> None:
        r = viewer_client.post(
            f"/api/v1/orgs/{_ORG}/invites",
            json={"email": "new@example.com", "role": "member"},
        )
        assert r.status_code == 403

    def test_member_cannot_create_invite(self, member_client: TestClient) -> None:
        r = member_client.post(
            f"/api/v1/orgs/{_ORG}/invites",
            json={"email": "new@example.com", "role": "member"},
        )
        assert r.status_code == 403

    def test_cannot_invite_with_owner_role(self, admin_client: TestClient) -> None:
        with mock.patch("aegis.server.api.routers.invite._send_invite_email"):
            r = admin_client.post(
                f"/api/v1/orgs/{_ORG}/invites",
                json={"email": "new@example.com", "role": "owner"},
            )
        assert r.status_code == 400

    def test_invalid_role_rejected(self, admin_client: TestClient) -> None:
        with mock.patch("aegis.server.api.routers.invite._send_invite_email"):
            r = admin_client.post(
                f"/api/v1/orgs/{_ORG}/invites",
                json={"email": "new@example.com", "role": "superadmin"},
            )
        assert r.status_code == 400


# ── verify invite ─────────────────────────────────────────────────────────────


class TestGetInvite:
    def _public_client(self, conn: mock.AsyncMock) -> TestClient:
        fa = FastAPI()
        fa.include_router(invite_router.router)

        async def _conn() -> AsyncIterator[mock.AsyncMock]:
            yield conn

        fa.dependency_overrides[get_db_conn] = _conn
        return TestClient(fa, raise_server_exceptions=False)

    def test_valid_token_returns_invite_info(self, conn: mock.AsyncMock) -> None:
        c = self._public_client(conn)
        r = c.get(f"/api/v1/invites/{_TOKEN}")
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "new@example.com"
        assert body["role"] == "member"

    def test_unknown_token_returns_404(self, conn: mock.AsyncMock) -> None:
        conn.fetchrow.return_value = None
        c = self._public_client(conn)
        r = c.get("/api/v1/invites/unknown-token")
        assert r.status_code == 404

    def test_expired_token_returns_410(self, conn: mock.AsyncMock) -> None:
        conn.fetchrow.return_value = _invite_row(expires_at=datetime.now(UTC) - timedelta(days=1))
        c = self._public_client(conn)
        r = c.get(f"/api/v1/invites/{_TOKEN}")
        assert r.status_code == 410

    def test_already_accepted_returns_410(self, conn: mock.AsyncMock) -> None:
        conn.fetchrow.return_value = _invite_row(accepted_at=datetime.now(UTC))
        c = self._public_client(conn)
        r = c.get(f"/api/v1/invites/{_TOKEN}")
        assert r.status_code == 410


# ── accept invite ─────────────────────────────────────────────────────────────


class TestAcceptInvite:
    def _public_client(self, conn: mock.AsyncMock) -> TestClient:
        fa = FastAPI()
        fa.include_router(invite_router.router)

        async def _conn() -> AsyncIterator[mock.AsyncMock]:
            yield conn

        fa.dependency_overrides[get_db_conn] = _conn
        return TestClient(fa, raise_server_exceptions=False)

    def test_accept_creates_user_and_membership(self, conn: mock.AsyncMock) -> None:
        new_user_id = uuid.uuid4()
        # fetchrow calls in order: get_invite, get_user_by_email, check_membership
        conn.fetchrow.side_effect = [
            _invite_row(),  # invite lookup
            None,  # user doesn't exist yet → create
            None,  # no existing membership
        ]
        new_user_row = {
            "id": new_user_id,
            "email": "new@example.com",
            "password_hash": "x",
            "display_name": None,
            "is_active": True,
            "default_org_id": None,
            "last_login_at": None,
            "created_at": datetime.now(UTC),
        }
        # After side_effect exhausted, fetchrow returns user row for create
        conn.fetchrow.side_effect = [
            _invite_row(),
            None,
            new_user_row,
            None,
        ]
        c = self._public_client(conn)
        r = c.post(
            f"/api/v1/invites/{_TOKEN}/accept",
            json={"password": "SecurePass1234!", "display_name": "New User"},
        )
        assert r.status_code == 201
        body = r.json()
        assert "user_id" in body

    def test_accept_expired_invite_returns_410(self, conn: mock.AsyncMock) -> None:
        conn.fetchrow.return_value = _invite_row(expires_at=datetime.now(UTC) - timedelta(days=1))
        c = self._public_client(conn)
        r = c.post(
            f"/api/v1/invites/{_TOKEN}/accept",
            json={"password": "SecurePass1234!"},
        )
        assert r.status_code == 410

    def test_accept_short_password_rejected(self, conn: mock.AsyncMock) -> None:
        c = self._public_client(conn)
        r = c.post(
            f"/api/v1/invites/{_TOKEN}/accept",
            json={"password": "short"},
        )
        assert r.status_code == 422

    def test_accept_missing_token_returns_404(self, conn: mock.AsyncMock) -> None:
        conn.fetchrow.return_value = None
        c = self._public_client(conn)
        r = c.post(
            "/api/v1/invites/bad-token/accept",
            json={"password": "SecurePass1234!"},
        )
        assert r.status_code == 404


# ── RBAC: invite endpoint requires admin+ ────────────────────────────────────


class TestInviteRbacEnforcement:
    def test_operator_cannot_invite(self, conn: mock.AsyncMock) -> None:
        fa = FastAPI()
        fa.include_router(invite_router.router)
        u = _user_with_role("operator")
        fa.dependency_overrides[get_current_user] = lambda: u

        async def _conn() -> AsyncIterator[mock.AsyncMock]:
            yield conn

        fa.dependency_overrides[get_db_conn] = _conn
        with TestClient(fa, raise_server_exceptions=False) as c:
            r = c.post(
                f"/api/v1/orgs/{_ORG}/invites",
                json={"email": "x@y.com", "role": "member"},
            )
        assert r.status_code == 403

    def test_admin_can_invite(self, conn: mock.AsyncMock) -> None:
        fa = FastAPI()
        fa.include_router(invite_router.router)
        u = _user_with_role("admin")
        fa.dependency_overrides[get_current_user] = lambda: u

        async def _conn() -> AsyncIterator[mock.AsyncMock]:
            yield conn

        fa.dependency_overrides[get_db_conn] = _conn
        with (
            TestClient(fa, raise_server_exceptions=False) as c,
            mock.patch("aegis.server.api.routers.invite._send_invite_email"),
        ):
            r = c.post(
                f"/api/v1/orgs/{_ORG}/invites",
                json={"email": "x@y.com", "role": "member"},
            )
        assert r.status_code == 201

    def test_owner_can_invite(self, conn: mock.AsyncMock) -> None:
        fa = FastAPI()
        fa.include_router(invite_router.router)
        u = _user_with_role("owner")
        fa.dependency_overrides[get_current_user] = lambda: u

        async def _conn() -> AsyncIterator[mock.AsyncMock]:
            yield conn

        fa.dependency_overrides[get_db_conn] = _conn
        with (
            TestClient(fa, raise_server_exceptions=False) as c,
            mock.patch("aegis.server.api.routers.invite._send_invite_email"),
        ):
            r = c.post(
                f"/api/v1/orgs/{_ORG}/invites",
                json={"email": "x@y.com", "role": "viewer"},
            )
        assert r.status_code == 201
