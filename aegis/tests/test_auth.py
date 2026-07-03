"""C1-2 auth tests — RevokedTokenRepository + auth router e2e.

Non-smoke (§1): none (password_service / jwt_service tests removed — logic now in obase.auth).
Smoke (§3, §4): require RUN_SMOKE=1 + Docker.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

# ── JWT secret for all tests in this module ─────────────────────────────────
TEST_JWT_SECRET = "test-jwt-secret-32-chars-long-ok!"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_EMAIL = "testauth@example.com"
INACTIVE_EMAIL = "inactive-auth@example.com"

os.environ.setdefault("AEGIS_JWT_SECRET", TEST_JWT_SECRET)
os.environ.setdefault("AEGIS_JWT_REFRESH_SECURE", "false")  # tests run over HTTP

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
SMOKE_SKIP = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")


# ════════════════════════════════════════════════════════════════════════════
# Smoke fixtures (shared by §3 + §4)
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    if not RUN_SMOKE:
        pytest.skip("set RUN_SMOKE=1 to run")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("timescale/timescaledb:2.26.3-pg18") as pg:
        yield pg


@pytest.fixture(scope="module")
def auth_dsn(pg_container: Any) -> str:
    return pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
async def auth_conn(auth_dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:
    """Function-scoped direct connection — one per test, same event loop."""
    from aegis.server.persistence.migrations import apply_migrations

    conn: asyncpg.Connection = await asyncpg.connect(auth_dsn)
    await apply_migrations(conn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def minimal_auth_app() -> Any:
    """Minimal FastAPI app with only auth router; no lifespan pool management."""
    os.environ["AEGIS_JWT_SECRET"] = TEST_JWT_SECRET
    from aegis.server.runtime.config import get_settings

    get_settings.cache_clear()

    from fastapi import FastAPI

    from aegis.server.api.routers.auth import router as auth_router

    app = FastAPI()
    app.include_router(auth_router)
    return app


@pytest.fixture
async def test_user_data(auth_conn: asyncpg.Connection) -> Any:
    """Idempotent: get-or-create active test user with default-org membership."""
    from obase.auth import argon2_hash

    from aegis.server.models import Role
    from aegis.server.repositories import MembershipRepository, OrgRepository, UserRepository

    user_repo = UserRepository(auth_conn)
    org_repo = OrgRepository(auth_conn)
    membership_repo = MembershipRepository(auth_conn)

    user = await user_repo.get_by_email(TEST_EMAIL)
    if user is None:
        user = await user_repo.create(
            email=TEST_EMAIL,
            password_hash=argon2_hash(password=TEST_PASSWORD),
        )
        org = await org_repo.get_by_slug("default")
        if org:
            await membership_repo.add(user_id=user.id, org_id=org.id, role=Role.OWNER)
    return user


@pytest.fixture
async def inactive_user_data(auth_conn: asyncpg.Connection) -> Any:
    """Idempotent: get-or-create inactive test user."""
    from obase.auth import argon2_hash

    from aegis.server.repositories import UserRepository

    user_repo = UserRepository(auth_conn)
    user = await user_repo.get_by_email(INACTIVE_EMAIL)
    if user is None:
        user = await user_repo.create(
            email=INACTIVE_EMAIL,
            password_hash=argon2_hash(password=TEST_PASSWORD),
        )
        await user_repo.set_active(user.id, is_active=False)
    return user


@pytest.fixture
async def auth_client(
    minimal_auth_app: Any,
    auth_conn: asyncpg.Connection,
    test_user_data: Any,
    inactive_user_data: Any,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Function-scoped HTTP client — injects auth_conn via dependency override."""
    from aegis.server.api.deps import get_db_conn

    async def _override() -> AsyncGenerator[asyncpg.Connection, None]:
        yield auth_conn

    minimal_auth_app.dependency_overrides[get_db_conn] = _override
    transport = httpx.ASGITransport(app=minimal_auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    minimal_auth_app.dependency_overrides.clear()


# ════════════════════════════════════════════════════════════════════════════
# §4  RevokedTokenRepository  (4 smoke tests)
# ════════════════════════════════════════════════════════════════════════════


@SMOKE_SKIP
class TestRevokedTokenRepository:
    async def test_revoke_then_is_revoked_true(
        self, auth_conn: asyncpg.Connection, test_user_data: Any
    ) -> None:
        from aegis.server.repositories import RevokedTokenRepository

        repo = RevokedTokenRepository(auth_conn)
        jti = str(uuid4())
        future = datetime.now(UTC) + timedelta(days=30)
        await repo.revoke(jti=jti, user_id=test_user_data.id, expires_at=future)
        assert await repo.is_revoked(jti) is True

    async def test_not_revoked_returns_false(self, auth_conn: asyncpg.Connection) -> None:
        from aegis.server.repositories import RevokedTokenRepository

        repo = RevokedTokenRepository(auth_conn)
        assert await repo.is_revoked(str(uuid4())) is False

    async def test_revoke_idempotent(
        self, auth_conn: asyncpg.Connection, test_user_data: Any
    ) -> None:
        from aegis.server.repositories import RevokedTokenRepository

        repo = RevokedTokenRepository(auth_conn)
        jti = str(uuid4())
        future = datetime.now(UTC) + timedelta(days=30)
        await repo.revoke(jti=jti, user_id=test_user_data.id, expires_at=future)
        # Second revoke must not raise
        await repo.revoke(jti=jti, user_id=test_user_data.id, expires_at=future)
        assert await repo.is_revoked(jti) is True

    async def test_cleanup_expired_deletes_expired(
        self, auth_conn: asyncpg.Connection, test_user_data: Any
    ) -> None:
        from aegis.server.repositories import RevokedTokenRepository

        repo = RevokedTokenRepository(auth_conn)
        jti = str(uuid4())
        past = datetime.now(UTC) - timedelta(hours=1)
        await repo.revoke(jti=jti, user_id=test_user_data.id, expires_at=past)

        count = await repo.cleanup_expired()
        assert count >= 1
        assert await repo.is_revoked(jti) is False


# ════════════════════════════════════════════════════════════════════════════
# §3  auth router e2e  (12 smoke tests)
# ════════════════════════════════════════════════════════════════════════════


@SMOKE_SKIP
class TestAuthRouter:
    async def test_login_success_returns_access_token(self, auth_client: httpx.AsyncClient) -> None:
        resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0
        # Verify orgs embedded in token
        from obase.auth import jwt_verify_hs256

        payload = jwt_verify_hs256(token=data["access_token"], secret=TEST_JWT_SECRET)
        assert len(payload["orgs"]) > 0
        assert payload["orgs"][0]["slug"] == "default"

    async def test_login_wrong_password_401(self, auth_client: httpx.AsyncClient) -> None:
        resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": "wrong-password-here"},
        )
        assert resp.status_code == 401

    async def test_login_inactive_user_401(self, auth_client: httpx.AsyncClient) -> None:
        resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": INACTIVE_EMAIL, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 401

    async def test_login_nonexistent_email_401(self, auth_client: httpx.AsyncClient) -> None:
        resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "noone@nowhere.invalid", "password": TEST_PASSWORD},
        )
        assert resp.status_code == 401

    async def test_login_sets_httponly_refresh_cookie(self, auth_client: httpx.AsyncClient) -> None:
        resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "refresh_token=" in set_cookie
        assert "HttpOnly" in set_cookie

    async def test_refresh_with_valid_cookie_returns_new_access(
        self, auth_client: httpx.AsyncClient
    ) -> None:
        login_resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        refresh_cookie = login_resp.cookies.get("refresh_token")
        assert refresh_cookie

        resp = await auth_client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh_cookie},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        # Rotation: a new refresh cookie must be issued
        new_cookie = resp.cookies.get("refresh_token")
        assert new_cookie is not None
        assert new_cookie != refresh_cookie

    async def test_refresh_rotates_old_token_rejected(self, auth_client: httpx.AsyncClient) -> None:
        """After /refresh, replaying the old refresh token must return 401."""
        login_resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        old_cookie = login_resp.cookies.get("refresh_token")
        assert old_cookie

        refresh_resp = await auth_client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": old_cookie},
        )
        assert refresh_resp.status_code == 200

        # Replay the consumed (now-rotated) token
        replay_resp = await auth_client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": old_cookie},
        )
        assert replay_resp.status_code == 401

    async def test_refresh_revoked_token_401(
        self, auth_client: httpx.AsyncClient, auth_conn: asyncpg.Connection
    ) -> None:
        login_resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        refresh_cookie = login_resp.cookies.get("refresh_token")
        assert refresh_cookie

        # Manually revoke the token's jti
        from obase.auth import jwt_verify_hs256

        from aegis.server.repositories import RevokedTokenRepository

        payload = jwt_verify_hs256(token=refresh_cookie, secret=TEST_JWT_SECRET)
        repo = RevokedTokenRepository(auth_conn)
        await repo.revoke(
            jti=payload["jti"],
            user_id=UUID(payload["sub"]),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
        )

        resp = await auth_client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": refresh_cookie},
        )
        assert resp.status_code == 401

    async def test_refresh_no_cookie_401(self, auth_client: httpx.AsyncClient) -> None:
        resp = await auth_client.post("/api/v1/auth/refresh")
        assert resp.status_code == 401

    async def test_refresh_expired_cookie_401(self, auth_client: httpx.AsyncClient) -> None:
        import jwt as pyjwt

        expired_payload = {
            "sub": str(uuid4()),
            "jti": str(uuid4()),
            "type": "refresh",
            "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        }
        expired_token = pyjwt.encode(expired_payload, TEST_JWT_SECRET, algorithm="HS256")

        resp = await auth_client.post(
            "/api/v1/auth/refresh",
            cookies={"refresh_token": expired_token},
        )
        assert resp.status_code == 401

    async def test_logout_revokes_token_and_clears_cookie(
        self, auth_client: httpx.AsyncClient, auth_conn: asyncpg.Connection
    ) -> None:
        login_resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        refresh_cookie = login_resp.cookies.get("refresh_token")
        assert refresh_cookie

        logout_resp = await auth_client.post(
            "/api/v1/auth/logout",
            cookies={"refresh_token": refresh_cookie},
        )
        assert logout_resp.status_code == 204

        # Token must be in revoked_tokens table
        from obase.auth import jwt_verify_hs256

        from aegis.server.repositories import RevokedTokenRepository

        payload = jwt_verify_hs256(token=refresh_cookie, secret=TEST_JWT_SECRET)
        repo = RevokedTokenRepository(auth_conn)
        assert await repo.is_revoked(payload["jti"]) is True

    async def test_me_returns_user_from_token(self, auth_client: httpx.AsyncClient) -> None:
        login_resp = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        access_token = login_resp.json()["access_token"]

        resp = await auth_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == TEST_EMAIL
        assert len(data["orgs"]) > 0
        assert data["orgs"][0]["slug"] == "default"

    async def test_me_no_token_401(self, auth_client: httpx.AsyncClient) -> None:
        resp = await auth_client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    async def test_change_password_flow(self, auth_client: httpx.AsyncClient) -> None:
        login = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        )
        assert login.status_code == 200
        hdr = {"Authorization": f"Bearer {login.json()['access_token']}"}
        new_pw = "brand-new-password-9"

        # Wrong current password -> 400.
        bad = await auth_client.post(
            "/api/v1/auth/password",
            headers=hdr,
            json={"current_password": "not-my-password", "new_password": new_pw},
        )
        assert bad.status_code == 400

        # Too-short new password -> 422 (min_length=8).
        short = await auth_client.post(
            "/api/v1/auth/password",
            headers=hdr,
            json={"current_password": TEST_PASSWORD, "new_password": "short"},
        )
        assert short.status_code == 422

        # Correct current password -> 204, old password stops working, new one works.
        ok = await auth_client.post(
            "/api/v1/auth/password",
            headers=hdr,
            json={"current_password": TEST_PASSWORD, "new_password": new_pw},
        )
        assert ok.status_code == 200
        old = await auth_client.post(
            "/api/v1/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD}
        )
        assert old.status_code == 401
        new = await auth_client.post(
            "/api/v1/auth/login", json={"email": TEST_EMAIL, "password": new_pw}
        )
        assert new.status_code == 200

        # Restore the seeded password so sibling tests are unaffected.
        restore = await auth_client.post(
            "/api/v1/auth/password",
            headers={"Authorization": f"Bearer {new.json()['access_token']}"},
            json={"current_password": new_pw, "new_password": TEST_PASSWORD},
        )
        assert restore.status_code == 200

    async def test_change_password_requires_auth(self, auth_client: httpx.AsyncClient) -> None:
        resp = await auth_client.post(
            "/api/v1/auth/password",
            json={"current_password": TEST_PASSWORD, "new_password": "whatever-8"},
        )
        assert resp.status_code == 401
