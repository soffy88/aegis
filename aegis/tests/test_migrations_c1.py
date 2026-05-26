"""Tests for C1-1 migrations (006 ALTER upgrade + 008 backfill). Requires RUN_SMOKE=1."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from typing import Any

import asyncpg
import pytest

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16") as pg:
        yield pg


@pytest.fixture
async def pg_conn(pg_container: Any) -> AsyncGenerator[asyncpg.Connection, None]:
    dsn = pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(conn)
    try:
        yield conn
    finally:
        await conn.close()


async def test_migration_006_orgs_has_slug(pg_conn: asyncpg.Connection) -> None:
    col = await pg_conn.fetchrow(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'orgs' AND column_name = 'slug'"
    )
    assert col is not None


async def test_migration_006_orgs_slug_not_null(pg_conn: asyncpg.Connection) -> None:
    col = await pg_conn.fetchrow(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'orgs' AND column_name = 'slug'"
    )
    assert col["is_nullable"] == "NO"


async def test_migration_006_orgs_plan_check(pg_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await pg_conn.execute(
            "INSERT INTO orgs (slug, name, plan) VALUES ('bad-plan', 'Bad', 'platinum')"
        )


async def test_migration_006_users_password_hash_renamed(pg_conn: asyncpg.Connection) -> None:
    col = await pg_conn.fetchrow(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'password_hash'"
    )
    assert col is not None
    old = await pg_conn.fetchrow(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'hashed_password'"
    )
    assert old is None


async def test_migration_006_users_new_columns(pg_conn: asyncpg.Connection) -> None:
    for col_name in ("display_name", "is_active", "last_login_at"):
        col = await pg_conn.fetchrow(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = $1",
            col_name,
        )
        assert col is not None, f"missing column: {col_name}"


async def test_migration_006_memberships_operator_accepted(pg_conn: asyncpg.Connection) -> None:
    uid = await pg_conn.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ('op@test.io', 'x') RETURNING id"
    )
    oid = await pg_conn.fetchval(
        "INSERT INTO orgs (slug, name) VALUES ('op-test', 'OpTest') RETURNING id"
    )
    await pg_conn.execute(
        "INSERT INTO org_memberships (org_id, user_id, role) VALUES ($1, $2, 'operator')",
        oid,
        uid,
    )


async def test_migration_006_memberships_invalid_role_rejected(pg_conn: asyncpg.Connection) -> None:
    uid = await pg_conn.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ('bad@test.io', 'x') RETURNING id"
    )
    oid = await pg_conn.fetchval(
        "INSERT INTO orgs (slug, name) VALUES ('bad-role', 'BadRole') RETURNING id"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await pg_conn.execute(
            "INSERT INTO org_memberships (org_id, user_id, role) VALUES ($1, $2, 'superadmin')",
            oid,
            uid,
        )


async def test_migration_006_memberships_joined_at(pg_conn: asyncpg.Connection) -> None:
    col = await pg_conn.fetchrow(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'org_memberships' AND column_name = 'joined_at'"
    )
    assert col is not None


async def test_migration_006_projects_new_columns(pg_conn: asyncpg.Connection) -> None:
    for col_name in ("slug", "display_name", "docker_labels", "config", "archived_at"):
        col = await pg_conn.fetchrow(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'projects' AND column_name = $1",
            col_name,
        )
        assert col is not None, f"missing column: {col_name}"


async def test_migration_006_projects_slug_unique(pg_conn: asyncpg.Connection) -> None:
    con = await pg_conn.fetchrow(
        "SELECT conname FROM pg_constraint WHERE conname = 'projects_org_slug_unique'"
    )
    assert con is not None


async def test_migration_008_default_org_slug(pg_conn: asyncpg.Connection) -> None:
    org = await pg_conn.fetchrow("SELECT * FROM orgs WHERE slug = 'default'")
    assert org is not None
    assert org["name"] == "default"


async def test_migration_008_default_project_slug(pg_conn: asyncpg.Connection) -> None:
    org = await pg_conn.fetchrow("SELECT * FROM orgs WHERE slug = 'default'")
    proj = await pg_conn.fetchrow(
        "SELECT * FROM projects WHERE org_id = $1 AND slug = 'default'", org["id"]
    )
    assert proj is not None
    assert proj["display_name"] is not None


async def test_migration_006_does_not_break_existing_data(pg_conn: asyncpg.Connection) -> None:
    org = await pg_conn.fetchrow("SELECT * FROM orgs WHERE slug = 'default'")
    assert org is not None
    assert org["name"]
    proj = await pg_conn.fetchrow(
        "SELECT * FROM projects WHERE org_id = $1 AND slug = 'default'", org["id"]
    )
    assert proj is not None
    assert proj["name"]


async def test_migrations_idempotent(pg_conn: asyncpg.Connection) -> None:
    from aegis.server.persistence.migrations import apply_migrations

    n = await apply_migrations(pg_conn)
    assert n == 0
