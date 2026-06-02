"""Tests for AlertRuleRepository — C2-2. RUN_SMOKE=1."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import asyncpg
import pytest

from aegis.server.repositories.alert_rule_repository import AlertRuleRepository
from aegis.server.schemas.alerting import AlertRuleCreate, AlertRuleUpdate

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PROJ = uuid.UUID("22222222-2222-2222-2222-222222222222")
_USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("timescale/timescaledb:2.26.3-pg18") as pg:
        yield pg


@pytest.fixture
async def conn(pg_container: Any) -> AsyncGenerator[asyncpg.Connection, None]:
    dsn = pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    c = await asyncpg.connect(dsn)
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(c)
    # Seed default org, project, user so FK constraints pass
    await c.execute(
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'test', 'Test', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG,
    )
    await c.execute(
        "INSERT INTO projects (id, org_id, name, slug, display_name)"
        " VALUES ($1,$2,'P','p','P') ON CONFLICT DO NOTHING",
        _PROJ,
        _ORG,
    )
    await c.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1,'u@t.com','h')"
        " ON CONFLICT DO NOTHING",
        _USER,
    )
    try:
        yield c
    finally:
        await c.close()


def _rule(name: str = "cpu-warn", **kwargs: Any) -> AlertRuleCreate:
    return AlertRuleCreate(
        name=name,
        metric="container.cpu.percent",
        threshold_warn=70.0,
        threshold_critical=90.0,
        **kwargs,
    )


class TestAlertRuleRepository:
    async def test_create_rule(self, conn: asyncpg.Connection) -> None:
        repo = AlertRuleRepository(conn)
        rule = await repo.create(org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule())
        assert rule.name == "cpu-warn"
        assert rule.threshold_warn == 70.0
        assert rule.threshold_critical == 90.0
        assert rule.enabled is True

    async def test_create_with_no_threshold_fails(self, conn: asyncpg.Connection) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="at least one"):
            AlertRuleCreate(name="bad", metric="m")

    async def test_create_duplicate_name_fails(self, conn: asyncpg.Connection) -> None:
        import asyncpg as apg

        repo = AlertRuleRepository(conn)
        await repo.create(org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule("dup-test"))
        with pytest.raises(apg.UniqueViolationError):
            await repo.create(
                org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule("dup-test")
            )

    async def test_get_by_id(self, conn: asyncpg.Connection) -> None:
        repo = AlertRuleRepository(conn)
        created = await repo.create(
            org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule("get-test")
        )
        found = await repo.get(rule_id=created.rule_id, org_id=_ORG)
        assert found is not None
        assert found.rule_id == created.rule_id
        assert found.metric == "container.cpu.percent"

    async def test_get_wrong_org_returns_none(self, conn: asyncpg.Connection) -> None:
        repo = AlertRuleRepository(conn)
        created = await repo.create(
            org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule("org-test")
        )
        other_org = uuid.uuid4()
        assert await repo.get(rule_id=created.rule_id, org_id=other_org) is None

    async def test_list_by_project_enabled_only(self, conn: asyncpg.Connection) -> None:
        repo = AlertRuleRepository(conn)
        await repo.create(
            org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule("list-enabled")
        )
        await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            created_by=_USER,
            data=_rule("list-disabled", enabled=False),
        )
        all_rules = await repo.list_by_project(org_id=_ORG, project_id=_PROJ)
        enabled = await repo.list_by_project(org_id=_ORG, project_id=_PROJ, enabled_only=True)
        assert len(enabled) < len(all_rules)
        assert all(r.enabled for r in enabled)

    async def test_update_partial(self, conn: asyncpg.Connection) -> None:
        repo = AlertRuleRepository(conn)
        created = await repo.create(
            org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule("upd-test")
        )
        updated = await repo.update(
            rule_id=created.rule_id,
            org_id=_ORG,
            data=AlertRuleUpdate(threshold_warn=80.0),
        )
        assert updated is not None
        assert updated.threshold_warn == 80.0
        assert updated.threshold_critical == 90.0  # unchanged

    async def test_update_disable(self, conn: asyncpg.Connection) -> None:
        repo = AlertRuleRepository(conn)
        created = await repo.create(
            org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule("dis-test")
        )
        updated = await repo.update(
            rule_id=created.rule_id, org_id=_ORG, data=AlertRuleUpdate(enabled=False)
        )
        assert updated is not None
        assert updated.enabled is False

    async def test_delete(self, conn: asyncpg.Connection) -> None:
        repo = AlertRuleRepository(conn)
        created = await repo.create(
            org_id=_ORG, project_id=_PROJ, created_by=_USER, data=_rule("del-test")
        )
        assert await repo.delete(rule_id=created.rule_id, org_id=_ORG) is True
        assert await repo.get(rule_id=created.rule_id, org_id=_ORG) is None

    async def test_delete_nonexistent_returns_false(self, conn: asyncpg.Connection) -> None:
        repo = AlertRuleRepository(conn)
        assert await repo.delete(rule_id=uuid.uuid4(), org_id=_ORG) is False
