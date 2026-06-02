"""C1-3 users /me router smoke tests. RUN_SMOKE=1 required."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from aegis.server.repositories import OrgRepository, UserRepository

TEST_JWT_SECRET = "test-jwt-secret-32-chars-long-ok!"
os.environ.setdefault("AEGIS_JWT_SECRET", TEST_JWT_SECRET)
os.environ.setdefault("AEGIS_JWT_REFRESH_SECURE", "false")

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
SMOKE_SKIP = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")


# ── helpers ───────────────────────────────────────────────────────────────────


async def make_user(conn: asyncpg.Connection, email: str) -> Any:
    return await UserRepository(conn).create(email=email, password_hash="$argon2id$dummy$")


async def make_org(conn: asyncpg.Connection, slug: str) -> Any:
    return await OrgRepository(conn).create(slug=slug, name=f"Org {slug}")


def make_token(user_id: UUID, email: str, orgs: list[dict]) -> str:
    from obase.auth import jwt_sign_hs256

    from aegis.server.runtime.config import get_settings

    settings = get_settings()
    return jwt_sign_hs256(
        payload={"sub": str(user_id), "email": email, "orgs": orgs, "type": "access"},
        secret=settings.jwt_secret,
        expires_in_seconds=settings.jwt_access_ttl_minutes * 60,
    )


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def org_in_token(org_id: UUID, slug: str, role: str) -> dict:
    return {"org_id": str(org_id), "slug": slug, "role": role}


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    if not RUN_SMOKE:
        pytest.skip("set RUN_SMOKE=1 to run")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("timescale/timescaledb:2.26.3-pg18") as pg:
        yield pg


@pytest.fixture(scope="module")
def users_dsn(pg_container: Any) -> str:
    return pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
async def users_conn(users_dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:
    from aegis.server.persistence.migrations import apply_migrations

    conn: asyncpg.Connection = await asyncpg.connect(users_dsn)
    await apply_migrations(conn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def minimal_users_app() -> Any:
    from aegis.server.runtime.config import get_settings

    get_settings.cache_clear()
    from fastapi import FastAPI

    from aegis.server.api.routers.users import router as users_router

    app = FastAPI()
    app.include_router(users_router)
    return app


@pytest.fixture
async def users_client(
    minimal_users_app: Any, users_conn: asyncpg.Connection
) -> AsyncGenerator[httpx.AsyncClient, None]:
    from aegis.server.api.deps import get_db_conn

    async def _override() -> AsyncGenerator[asyncpg.Connection, None]:
        yield users_conn

    minimal_users_app.dependency_overrides[get_db_conn] = _override
    transport = httpx.ASGITransport(app=minimal_users_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    minimal_users_app.dependency_overrides.clear()


# ════════════════════════════════════════════════════════════════════════════
# §6.4  users /me (tests 31-35)
# ════════════════════════════════════════════════════════════════════════════


@SMOKE_SKIP
class TestUsersRouter:
    async def test_get_my_profile_200(
        self, users_client: httpx.AsyncClient, users_conn: asyncpg.Connection
    ) -> None:
        """GET /me returns profile for the authenticated user."""
        user = await make_user(users_conn, f"profile-{uuid4().hex[:8]}@test.com")
        token = make_token(user.id, user.email, [])

        resp = await users_client.get("/api/v1/users/me", headers=bearer(token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(user.id)
        assert data["email"] == user.email
        assert data["is_active"] is True

    async def test_update_display_name_200(
        self, users_client: httpx.AsyncClient, users_conn: asyncpg.Connection
    ) -> None:
        """PATCH /me with display_name updates the profile."""
        user = await make_user(users_conn, f"display-{uuid4().hex[:8]}@test.com")
        token = make_token(user.id, user.email, [])

        resp = await users_client.patch(
            "/api/v1/users/me",
            json={"display_name": "Alice Smith"},
            headers=bearer(token),
        )

        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Alice Smith"

    async def test_update_default_org_id_must_belong_to_user_400(
        self, users_client: httpx.AsyncClient, users_conn: asyncpg.Connection
    ) -> None:
        """PATCH /me with an org the user doesn't belong to returns 400."""
        user = await make_user(users_conn, f"noorg-{uuid4().hex[:8]}@test.com")
        other_org = await make_org(users_conn, f"other-{uuid4().hex[:8]}")
        # token has no org memberships — user does not belong to other_org
        token = make_token(user.id, user.email, [])

        resp = await users_client.patch(
            "/api/v1/users/me",
            json={"default_org_id": str(other_org.id)},
            headers=bearer(token),
        )

        assert resp.status_code == 400
        assert "default_org_id" in resp.json()["detail"]

    async def test_update_default_org_id_to_owned_org_200(
        self, users_client: httpx.AsyncClient, users_conn: asyncpg.Connection
    ) -> None:
        """PATCH /me with an org the user belongs to sets default_org_id."""
        user = await make_user(users_conn, f"myorg-{uuid4().hex[:8]}@test.com")
        org = await make_org(users_conn, f"mine-{uuid4().hex[:8]}")
        # embed membership in token so org_by_id() returns a hit
        token = make_token(user.id, user.email, [org_in_token(org.id, org.slug, "owner")])

        resp = await users_client.patch(
            "/api/v1/users/me",
            json={"default_org_id": str(org.id)},
            headers=bearer(token),
        )

        assert resp.status_code == 200
        assert resp.json()["default_org_id"] == str(org.id)

    async def test_patch_profile_no_token_401(self, users_client: httpx.AsyncClient) -> None:
        """PATCH /me without a token returns 401."""
        resp = await users_client.patch(
            "/api/v1/users/me",
            json={"display_name": "Ghost"},
        )

        assert resp.status_code == 401
