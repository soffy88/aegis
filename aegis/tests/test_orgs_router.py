"""C1-3 orgs router e2e smoke tests. RUN_SMOKE=1 required."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from aegis.server.models import Role
from aegis.server.repositories import MembershipRepository, OrgRepository, UserRepository

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


async def add_member(conn: asyncpg.Connection, user_id: UUID, org_id: UUID, role: Role) -> None:
    await MembershipRepository(conn).add(user_id=user_id, org_id=org_id, role=role)


def make_token(user_id: UUID, email: str, orgs: list[dict]) -> str:
    from aegis.server.auth.jwt_service import create_access_token

    token, _ = create_access_token(user_id=user_id, email=email, orgs=orgs)
    return token


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

    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture(scope="module")
def orgs_dsn(pg_container: Any) -> str:
    return pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
async def orgs_conn(orgs_dsn: str) -> AsyncGenerator[asyncpg.Connection, None]:
    from aegis.server.persistence.migrations import apply_migrations

    conn: asyncpg.Connection = await asyncpg.connect(orgs_dsn)
    await apply_migrations(conn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def minimal_orgs_app() -> Any:
    from aegis.server.runtime.config import get_settings

    get_settings.cache_clear()
    from fastapi import FastAPI

    from aegis.server.api.routers.orgs import router as orgs_router
    from aegis.server.api.routers.users import router as users_router

    app = FastAPI()
    app.include_router(orgs_router)
    app.include_router(users_router)
    return app


@pytest.fixture
async def orgs_client(
    minimal_orgs_app: Any, orgs_conn: asyncpg.Connection
) -> AsyncGenerator[httpx.AsyncClient, None]:
    from aegis.server.api.deps import get_db_conn

    async def _override() -> AsyncGenerator[asyncpg.Connection, None]:
        yield orgs_conn

    minimal_orgs_app.dependency_overrides[get_db_conn] = _override
    transport = httpx.ASGITransport(app=minimal_orgs_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    minimal_orgs_app.dependency_overrides.clear()


# ════════════════════════════════════════════════════════════════════════════
# §6.2  orgs CRUD (tests 11-25)
# ════════════════════════════════════════════════════════════════════════════


@SMOKE_SKIP
class TestOrgsRouterCRUD:
    async def test_create_org_makes_creator_owner(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        user = await make_user(orgs_conn, f"owner-{uuid4().hex[:8]}@test.com")
        token = make_token(user.id, user.email, [])
        slug = f"new-{uuid4().hex[:8]}"

        resp = await orgs_client.post(
            "/api/v1/orgs",
            json={"slug": slug, "name": "New Org"},
            headers=bearer(token),
        )
        assert resp.status_code == 201
        org_id = UUID(resp.json()["id"])

        m = await MembershipRepository(orgs_conn).get(user_id=user.id, org_id=org_id)
        assert m is not None
        assert m.role == Role.OWNER

    async def test_create_org_duplicate_slug_409(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        slug = f"dup-{uuid4().hex[:8]}"
        await make_org(orgs_conn, slug)
        user = await make_user(orgs_conn, f"u-{uuid4().hex[:8]}@test.com")
        token = make_token(user.id, user.email, [])

        resp = await orgs_client.post(
            "/api/v1/orgs",
            json={"slug": slug, "name": "Dupe"},
            headers=bearer(token),
        )
        assert resp.status_code == 409

    async def test_get_org_as_member_200(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"get-{uuid4().hex[:8]}")
        user = await make_user(orgs_conn, f"u-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, user.id, org.id, Role.VIEWER)
        token = make_token(user.id, user.email, [org_in_token(org.id, org.slug, "viewer")])

        resp = await orgs_client.get(f"/api/v1/orgs/{org.id}", headers=bearer(token))
        assert resp.status_code == 200
        assert resp.json()["slug"] == org.slug

    async def test_get_org_non_member_403(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"priv-{uuid4().hex[:8]}")
        user = await make_user(orgs_conn, f"u-{uuid4().hex[:8]}@test.com")
        token = make_token(user.id, user.email, [])  # no orgs in token

        resp = await orgs_client.get(f"/api/v1/orgs/{org.id}", headers=bearer(token))
        assert resp.status_code == 403

    async def test_update_org_as_admin_200(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"upd-{uuid4().hex[:8]}")
        user = await make_user(orgs_conn, f"u-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, user.id, org.id, Role.ADMIN)
        token = make_token(user.id, user.email, [org_in_token(org.id, org.slug, "admin")])

        resp = await orgs_client.patch(
            f"/api/v1/orgs/{org.id}",
            json={"name": "Updated Name"},
            headers=bearer(token),
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"

    async def test_update_org_as_member_403(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"upd2-{uuid4().hex[:8]}")
        user = await make_user(orgs_conn, f"u-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, user.id, org.id, Role.MEMBER)
        token = make_token(user.id, user.email, [org_in_token(org.id, org.slug, "member")])

        resp = await orgs_client.patch(
            f"/api/v1/orgs/{org.id}",
            json={"name": "Attempt"},
            headers=bearer(token),
        )
        assert resp.status_code == 403

    async def test_delete_org_as_owner_204(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"del-{uuid4().hex[:8]}")
        user = await make_user(orgs_conn, f"u-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, user.id, org.id, Role.OWNER)
        token = make_token(user.id, user.email, [org_in_token(org.id, org.slug, "owner")])

        resp = await orgs_client.delete(f"/api/v1/orgs/{org.id}", headers=bearer(token))
        assert resp.status_code == 204

        assert await OrgRepository(orgs_conn).get_by_id(org.id) is None

    async def test_delete_org_as_admin_403(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"del2-{uuid4().hex[:8]}")
        user = await make_user(orgs_conn, f"u-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, user.id, org.id, Role.ADMIN)
        token = make_token(user.id, user.email, [org_in_token(org.id, org.slug, "admin")])

        resp = await orgs_client.delete(f"/api/v1/orgs/{org.id}", headers=bearer(token))
        assert resp.status_code == 403

    async def test_list_members_as_viewer_200(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"mem-{uuid4().hex[:8]}")
        owner = await make_user(orgs_conn, f"o-{uuid4().hex[:8]}@test.com")
        viewer = await make_user(orgs_conn, f"v-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, owner.id, org.id, Role.OWNER)
        await add_member(orgs_conn, viewer.id, org.id, Role.VIEWER)
        token = make_token(viewer.id, viewer.email, [org_in_token(org.id, org.slug, "viewer")])

        resp = await orgs_client.get(f"/api/v1/orgs/{org.id}/members", headers=bearer(token))
        assert resp.status_code == 200
        emails = {m["email"] for m in resp.json()}
        assert owner.email in emails
        assert viewer.email in emails

    async def test_invite_member_as_admin_201(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"inv-{uuid4().hex[:8]}")
        admin = await make_user(orgs_conn, f"a-{uuid4().hex[:8]}@test.com")
        target = await make_user(orgs_conn, f"t-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, admin.id, org.id, Role.ADMIN)
        token = make_token(admin.id, admin.email, [org_in_token(org.id, org.slug, "admin")])

        resp = await orgs_client.post(
            f"/api/v1/orgs/{org.id}/members",
            json={"email": target.email, "role": "member"},
            headers=bearer(token),
        )
        assert resp.status_code == 201
        m = await MembershipRepository(orgs_conn).get(user_id=target.id, org_id=org.id)
        assert m is not None
        assert m.role == Role.MEMBER

    async def test_invite_member_as_member_403(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"inv2-{uuid4().hex[:8]}")
        member = await make_user(orgs_conn, f"m-{uuid4().hex[:8]}@test.com")
        target = await make_user(orgs_conn, f"t2-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, member.id, org.id, Role.MEMBER)
        token = make_token(member.id, member.email, [org_in_token(org.id, org.slug, "member")])

        resp = await orgs_client.post(
            f"/api/v1/orgs/{org.id}/members",
            json={"email": target.email, "role": "viewer"},
            headers=bearer(token),
        )
        assert resp.status_code == 403

    async def test_invite_owner_role_400(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"inv3-{uuid4().hex[:8]}")
        admin = await make_user(orgs_conn, f"a2-{uuid4().hex[:8]}@test.com")
        target = await make_user(orgs_conn, f"t3-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, admin.id, org.id, Role.ADMIN)
        token = make_token(admin.id, admin.email, [org_in_token(org.id, org.slug, "admin")])

        resp = await orgs_client.post(
            f"/api/v1/orgs/{org.id}/members",
            json={"email": target.email, "role": "owner"},
            headers=bearer(token),
        )
        assert resp.status_code == 400

    async def test_remove_member_as_admin_204(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"rm-{uuid4().hex[:8]}")
        admin = await make_user(orgs_conn, f"a3-{uuid4().hex[:8]}@test.com")
        target = await make_user(orgs_conn, f"t4-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, admin.id, org.id, Role.ADMIN)
        await add_member(orgs_conn, target.id, org.id, Role.MEMBER)
        token = make_token(admin.id, admin.email, [org_in_token(org.id, org.slug, "admin")])

        resp = await orgs_client.delete(
            f"/api/v1/orgs/{org.id}/members/{target.id}", headers=bearer(token)
        )
        assert resp.status_code == 204
        assert await MembershipRepository(orgs_conn).get(user_id=target.id, org_id=org.id) is None

    async def test_remove_self_400(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"self-{uuid4().hex[:8]}")
        admin = await make_user(orgs_conn, f"a4-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, admin.id, org.id, Role.ADMIN)
        token = make_token(admin.id, admin.email, [org_in_token(org.id, org.slug, "admin")])

        resp = await orgs_client.delete(
            f"/api/v1/orgs/{org.id}/members/{admin.id}", headers=bearer(token)
        )
        assert resp.status_code == 400

    async def test_remove_only_owner_400(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"solo-{uuid4().hex[:8]}")
        owner = await make_user(orgs_conn, f"o2-{uuid4().hex[:8]}@test.com")
        admin = await make_user(orgs_conn, f"a5-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, owner.id, org.id, Role.OWNER)
        await add_member(orgs_conn, admin.id, org.id, Role.ADMIN)
        token = make_token(admin.id, admin.email, [org_in_token(org.id, org.slug, "admin")])

        resp = await orgs_client.delete(
            f"/api/v1/orgs/{org.id}/members/{owner.id}", headers=bearer(token)
        )
        assert resp.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# §6.3  role management (tests 26-30)
# ════════════════════════════════════════════════════════════════════════════


@SMOKE_SKIP
class TestOrgsRouterRoles:
    async def test_change_member_role_as_owner_200(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"cr-{uuid4().hex[:8]}")
        owner = await make_user(orgs_conn, f"o3-{uuid4().hex[:8]}@test.com")
        member = await make_user(orgs_conn, f"m2-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, owner.id, org.id, Role.OWNER)
        await add_member(orgs_conn, member.id, org.id, Role.MEMBER)
        token = make_token(owner.id, owner.email, [org_in_token(org.id, org.slug, "owner")])

        resp = await orgs_client.patch(
            f"/api/v1/orgs/{org.id}/members/{member.id}",
            json={"role": "admin"},
            headers=bearer(token),
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    async def test_admin_cannot_change_owner_role_403(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"cr2-{uuid4().hex[:8]}")
        owner = await make_user(orgs_conn, f"o4-{uuid4().hex[:8]}@test.com")
        admin = await make_user(orgs_conn, f"a6-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, owner.id, org.id, Role.OWNER)
        await add_member(orgs_conn, admin.id, org.id, Role.ADMIN)
        token = make_token(admin.id, admin.email, [org_in_token(org.id, org.slug, "admin")])

        resp = await orgs_client.patch(
            f"/api/v1/orgs/{org.id}/members/{owner.id}",
            json={"role": "admin"},
            headers=bearer(token),
        )
        assert resp.status_code == 403

    async def test_demote_only_owner_400(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"dem-{uuid4().hex[:8]}")
        owner = await make_user(orgs_conn, f"o5-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, owner.id, org.id, Role.OWNER)
        token = make_token(owner.id, owner.email, [org_in_token(org.id, org.slug, "owner")])

        resp = await orgs_client.patch(
            f"/api/v1/orgs/{org.id}/members/{owner.id}",
            json={"role": "admin"},
            headers=bearer(token),
        )
        assert resp.status_code == 400

    async def test_transfer_ownership_as_owner_204_swaps_roles(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"tr-{uuid4().hex[:8]}")
        owner = await make_user(orgs_conn, f"o6-{uuid4().hex[:8]}@test.com")
        new_owner = await make_user(orgs_conn, f"n-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, owner.id, org.id, Role.OWNER)
        await add_member(orgs_conn, new_owner.id, org.id, Role.MEMBER)
        token = make_token(owner.id, owner.email, [org_in_token(org.id, org.slug, "owner")])

        resp = await orgs_client.post(
            f"/api/v1/orgs/{org.id}/transfer-ownership",
            json={"new_owner_user_id": str(new_owner.id)},
            headers=bearer(token),
        )
        assert resp.status_code == 204

        mem_repo = MembershipRepository(orgs_conn)
        old_m = await mem_repo.get(user_id=owner.id, org_id=org.id)
        new_m = await mem_repo.get(user_id=new_owner.id, org_id=org.id)
        assert old_m is not None
        assert old_m.role == Role.ADMIN
        assert new_m is not None
        assert new_m.role == Role.OWNER

    async def test_transfer_ownership_target_not_in_org_404(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        org = await make_org(orgs_conn, f"tr2-{uuid4().hex[:8]}")
        owner = await make_user(orgs_conn, f"o7-{uuid4().hex[:8]}@test.com")
        outsider = await make_user(orgs_conn, f"out-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, owner.id, org.id, Role.OWNER)
        token = make_token(owner.id, owner.email, [org_in_token(org.id, org.slug, "owner")])

        resp = await orgs_client.post(
            f"/api/v1/orgs/{org.id}/transfer-ownership",
            json={"new_owner_user_id": str(outsider.id)},
            headers=bearer(token),
        )
        assert resp.status_code == 404

    async def test_change_role_to_owner_rejected_400(
        self, orgs_client: httpx.AsyncClient, orgs_conn: asyncpg.Connection
    ) -> None:
        """Privilege escalation guard: PATCH member role=owner must return 400.

        Without this check an ADMIN could bypass /transfer-ownership and create
        extra owners via change_member_role (sibling-check asymmetry).
        """
        org = await make_org(orgs_conn, f"sec-{uuid4().hex[:8]}")
        owner = await make_user(orgs_conn, f"os-{uuid4().hex[:8]}@test.com")
        target = await make_user(orgs_conn, f"ts-{uuid4().hex[:8]}@test.com")
        await add_member(orgs_conn, owner.id, org.id, Role.OWNER)
        await add_member(orgs_conn, target.id, org.id, Role.MEMBER)
        # Even an owner cannot use change_member_role to assign OWNER role.
        token = make_token(owner.id, owner.email, [org_in_token(org.id, org.slug, "owner")])

        resp = await orgs_client.patch(
            f"/api/v1/orgs/{org.id}/members/{target.id}",
            json={"role": "owner"},
            headers=bearer(token),
        )
        assert resp.status_code == 400
        assert "transfer-ownership" in resp.json()["detail"]
