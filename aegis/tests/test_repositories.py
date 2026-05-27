"""Tests for C1-1 repositories (OrgRepo, UserRepo, MembershipRepo, ProjectRepo). RUN_SMOKE=1."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from typing import Any

import asyncpg
import pytest

from aegis.server.models import Role
from aegis.server.repositories import (
    MembershipRepository,
    OrgRepository,
    ProjectRepository,
    UserRepository,
)

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture
async def conn(pg_container: Any) -> AsyncGenerator[asyncpg.Connection, None]:
    dsn = pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    c = await asyncpg.connect(dsn)
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(c)
    try:
        yield c
    finally:
        await c.close()


# ===== OrgRepository =====


class TestOrgRepository:
    async def test_create(self, conn: asyncpg.Connection) -> None:
        repo = OrgRepository(conn)
        org = await repo.create(slug="test-org", name="Test Org")
        assert org.slug == "test-org"
        assert org.name == "Test Org"
        assert org.plan == "free"

    async def test_get_by_id(self, conn: asyncpg.Connection) -> None:
        repo = OrgRepository(conn)
        org = await repo.create(slug="get-id", name="GetId")
        found = await repo.get_by_id(org.id)
        assert found is not None
        assert found.slug == "get-id"

    async def test_get_by_slug(self, conn: asyncpg.Connection) -> None:
        repo = OrgRepository(conn)
        org = await repo.create(slug="get-slug", name="GetSlug")
        found = await repo.get_by_slug("get-slug")
        assert found is not None
        assert found.id == org.id

    async def test_get_by_slug_not_found(self, conn: asyncpg.Connection) -> None:
        repo = OrgRepository(conn)
        assert await repo.get_by_slug("nonexistent") is None

    async def test_list_by_user(self, conn: asyncpg.Connection) -> None:
        repo = OrgRepository(conn)
        org = await repo.create(slug="list-user", name="ListUser")
        user_repo = UserRepository(conn)
        user = await user_repo.create(email="list@org.io", password_hash="h")
        mem_repo = MembershipRepository(conn)
        await mem_repo.add(user_id=user.id, org_id=org.id, role=Role.MEMBER)
        orgs = await repo.list_by_user(user.id)
        assert any(o.slug == "list-user" for o in orgs)

    async def test_update(self, conn: asyncpg.Connection) -> None:
        repo = OrgRepository(conn)
        org = await repo.create(slug="upd-org", name="Old")
        updated = await repo.update(org.id, name="New")
        assert updated is not None
        assert updated.name == "New"

    async def test_delete(self, conn: asyncpg.Connection) -> None:
        repo = OrgRepository(conn)
        org = await repo.create(slug="del-org", name="Del")
        assert await repo.delete(org.id) is True
        assert await repo.get_by_id(org.id) is None

    async def test_duplicate_slug_rejected(self, conn: asyncpg.Connection) -> None:
        repo = OrgRepository(conn)
        await repo.create(slug="dup-slug", name="Dup1")
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.create(slug="dup-slug", name="Dup2")


# ===== UserRepository =====


class TestUserRepository:
    async def test_create(self, conn: asyncpg.Connection) -> None:
        repo = UserRepository(conn)
        user = await repo.create(email="new@u.io", password_hash="hash123")
        assert user.email == "new@u.io"
        assert user.is_active is True

    async def test_get_by_id(self, conn: asyncpg.Connection) -> None:
        repo = UserRepository(conn)
        user = await repo.create(email="byid@u.io", password_hash="h")
        found = await repo.get_by_id(user.id)
        assert found is not None
        assert found.email == "byid@u.io"

    async def test_get_by_email(self, conn: asyncpg.Connection) -> None:
        repo = UserRepository(conn)
        user = await repo.create(email="byemail@u.io", password_hash="h")
        found = await repo.get_by_email("byemail@u.io")
        assert found is not None
        assert found.id == user.id

    async def test_get_by_email_not_found(self, conn: asyncpg.Connection) -> None:
        repo = UserRepository(conn)
        assert await repo.get_by_email("ghost@u.io") is None

    async def test_update_last_login(self, conn: asyncpg.Connection) -> None:
        repo = UserRepository(conn)
        user = await repo.create(email="login@u.io", password_hash="h")
        assert user.last_login_at is None
        await repo.update_last_login(user.id)
        updated = await repo.get_by_id(user.id)
        assert updated is not None
        assert updated.last_login_at is not None

    async def test_set_active(self, conn: asyncpg.Connection) -> None:
        repo = UserRepository(conn)
        user = await repo.create(email="active@u.io", password_hash="h")
        await repo.set_active(user.id, is_active=False)
        found = await repo.get_by_id(user.id)
        assert found is not None
        assert found.is_active is False

    async def test_update_display_name(self, conn: asyncpg.Connection) -> None:
        repo = UserRepository(conn)
        user = await repo.create(email="dn@u.io", password_hash="h")
        updated = await repo.update_display_name(user.id, "Alice")
        assert updated is not None
        assert updated.display_name == "Alice"

    async def test_duplicate_email_rejected(self, conn: asyncpg.Connection) -> None:
        repo = UserRepository(conn)
        await repo.create(email="dup@u.io", password_hash="h")
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.create(email="dup@u.io", password_hash="h2")


# ===== MembershipRepository =====


class TestMembershipRepository:
    async def _setup(self, conn: asyncpg.Connection) -> tuple:
        org_repo = OrgRepository(conn)
        user_repo = UserRepository(conn)
        import uuid

        slug = f"mem-{uuid.uuid4().hex[:6]}"
        org = await org_repo.create(slug=slug, name=slug)
        user = await user_repo.create(email=f"{slug}@m.io", password_hash="h")
        return org, user

    async def test_add(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        m = await repo.add(user_id=user.id, org_id=org.id, role=Role.ADMIN)
        assert m.role == Role.ADMIN
        assert m.org_id == org.id

    async def test_get(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        await repo.add(user_id=user.id, org_id=org.id, role=Role.MEMBER)
        found = await repo.get(user_id=user.id, org_id=org.id)
        assert found is not None
        assert found.role == Role.MEMBER

    async def test_get_not_found(self, conn: asyncpg.Connection) -> None:
        import uuid

        repo = MembershipRepository(conn)
        assert await repo.get(user_id=uuid.uuid4(), org_id=uuid.uuid4()) is None

    async def test_remove(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        await repo.add(user_id=user.id, org_id=org.id, role=Role.VIEWER)
        assert await repo.remove(user_id=user.id, org_id=org.id) is True
        assert await repo.get(user_id=user.id, org_id=org.id) is None

    async def test_list_by_user(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        await repo.add(user_id=user.id, org_id=org.id, role=Role.OPERATOR)
        memberships = await repo.list_by_user(user.id)
        assert len(memberships) >= 1
        assert any(m.role == Role.OPERATOR for m in memberships)

    async def test_list_by_org(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        await repo.add(user_id=user.id, org_id=org.id, role=Role.OWNER)
        results = await repo.list_by_org(org.id)
        assert len(results) >= 1
        m, u = results[0]
        assert m.role == Role.OWNER
        assert u.email == user.email

    async def test_update_role(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        await repo.add(user_id=user.id, org_id=org.id, role=Role.VIEWER)
        updated = await repo.update_role(user_id=user.id, org_id=org.id, new_role=Role.ADMIN)
        assert updated is not None
        assert updated.role == Role.ADMIN

    async def test_count_owners_in_org(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        await repo.add(user_id=user.id, org_id=org.id, role=Role.OWNER)
        count = await repo.count_owners_in_org(org.id)
        assert count == 1

    async def test_operator_role_accepted(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        m = await repo.add(user_id=user.id, org_id=org.id, role=Role.OPERATOR)
        assert m.role == Role.OPERATOR

    async def test_duplicate_membership_rejected(self, conn: asyncpg.Connection) -> None:
        org, user = await self._setup(conn)
        repo = MembershipRepository(conn)
        await repo.add(user_id=user.id, org_id=org.id, role=Role.MEMBER)
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.add(user_id=user.id, org_id=org.id, role=Role.ADMIN)


# ===== ProjectRepository =====


class TestProjectRepository:
    async def _org(self, conn: asyncpg.Connection) -> Any:
        import uuid

        repo = OrgRepository(conn)
        return await repo.create(slug=f"proj-{uuid.uuid4().hex[:6]}", name="ProjOrg")

    async def test_create(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        proj = await repo.create(
            org_id=org.id, slug="my-proj", name="my-proj", display_name="My Project"
        )
        assert proj.slug == "my-proj"
        assert proj.display_name == "My Project"

    async def test_get_by_id(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        proj = await repo.create(org_id=org.id, slug="get-proj", name="get-proj", display_name="G")
        found = await repo.get_by_id(proj.id)
        assert found is not None
        assert found.slug == "get-proj"

    async def test_get_by_org_and_slug(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        proj = await repo.create(org_id=org.id, slug="slug-q", name="slug-q", display_name="S")
        found = await repo.get_by_org_and_slug(org.id, "slug-q")
        assert found is not None
        assert found.id == proj.id

    async def test_get_by_org_and_slug_not_found(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        assert await repo.get_by_org_and_slug(org.id, "nope") is None

    async def test_list_by_org(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        await repo.create(org_id=org.id, slug="p1", name="p1", display_name="P1")
        await repo.create(org_id=org.id, slug="p2", name="p2", display_name="P2")
        projects = await repo.list_by_org(org.id)
        assert len(projects) == 2

    async def test_list_by_org_excludes_archived(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        proj = await repo.create(org_id=org.id, slug="arch", name="arch", display_name="Arch")
        await repo.archive(proj.id)
        projects = await repo.list_by_org(org.id)
        assert all(p.slug != "arch" for p in projects)

    async def test_list_by_org_includes_archived(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        proj = await repo.create(org_id=org.id, slug="arch2", name="arch2", display_name="A2")
        await repo.archive(proj.id)
        projects = await repo.list_by_org(org.id, include_archived=True)
        assert any(p.slug == "arch2" for p in projects)

    async def test_update_config(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        proj = await repo.create(org_id=org.id, slug="cfg", name="cfg", display_name="Cfg")
        updated = await repo.update_config(proj.id, {"key": "value"})
        assert updated is not None
        assert updated.config == {"key": "value"}

    async def test_archive(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        proj = await repo.create(org_id=org.id, slug="to-arch", name="to-arch", display_name="TA")
        assert await repo.archive(proj.id) is True
        found = await repo.get_by_id(proj.id)
        assert found is not None
        assert found.is_archived is True

    async def test_duplicate_slug_in_org_rejected(self, conn: asyncpg.Connection) -> None:
        org = await self._org(conn)
        repo = ProjectRepository(conn)
        await repo.create(org_id=org.id, slug="dup-p", name="dup-p", display_name="D")
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.create(org_id=org.id, slug="dup-p", name="dup-p2", display_name="D2")
