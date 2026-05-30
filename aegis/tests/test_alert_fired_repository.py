"""Tests for AlertFiredRepository — C2-2. RUN_SMOKE=1."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from aegis.server.repositories.alert_fired_repository import AlertFiredRepository
from aegis.server.repositories.alert_rule_repository import AlertRuleRepository
from aegis.server.schemas.alerting import AlertRuleCreate

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_ORG = uuid.UUID("33333333-3333-3333-3333-333333333333")
_PROJ = uuid.UUID("44444444-4444-4444-4444-444444444444")
_USER = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


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
    await c.execute(
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'fired-test', 'F', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG,
    )
    await c.execute(
        "INSERT INTO projects (id, org_id, name, slug, display_name)"
        " VALUES ($1,$2,'FP','fp','FP') ON CONFLICT DO NOTHING",
        _PROJ,
        _ORG,
    )
    await c.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1,'fired@t.com','h')"
        " ON CONFLICT DO NOTHING",
        _USER,
    )
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def rule_id(conn: asyncpg.Connection) -> uuid.UUID:
    repo = AlertRuleRepository(conn)
    rule = await repo.create(
        org_id=_ORG,
        project_id=_PROJ,
        created_by=_USER,
        data=AlertRuleCreate(
            name=f"fired-rule-{uuid.uuid4().hex[:6]}",
            metric="container.cpu.percent",
            threshold_warn=70.0,
            threshold_critical=90.0,
        ),
    )
    return rule.rule_id


class TestAlertFiredRepository:
    async def test_upsert_new(self, conn: asyncpg.Connection, rule_id: uuid.UUID) -> None:
        repo = AlertFiredRepository(conn)
        now = datetime.now(UTC)
        row, is_new = await repo.upsert_or_update_last_seen(
            rule_id=rule_id,
            org_id=_ORG,
            project_id=_PROJ,
            dedup_key=f"new-{uuid.uuid4().hex}",
            severity="warn",
            current_value=75.0,
            triggered_reason="cpu >= 70",
            now=now,
        )
        assert is_new is True
        assert row.severity == "warn"
        assert row.current_value == pytest.approx(75.0)

    async def test_upsert_existing_updates_last_seen(
        self, conn: asyncpg.Connection, rule_id: uuid.UUID
    ) -> None:
        repo = AlertFiredRepository(conn)
        key = f"dup-{uuid.uuid4().hex}"
        t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        t2 = datetime(2026, 1, 1, 12, 5, 0, tzinfo=UTC)
        _, is_new1 = await repo.upsert_or_update_last_seen(
            rule_id=rule_id,
            org_id=_ORG,
            project_id=_PROJ,
            dedup_key=key,
            severity="warn",
            current_value=75.0,
            triggered_reason="first",
            now=t1,
        )
        row2, is_new2 = await repo.upsert_or_update_last_seen(
            rule_id=rule_id,
            org_id=_ORG,
            project_id=_PROJ,
            dedup_key=key,
            severity="warn",
            current_value=80.0,
            triggered_reason="second",
            now=t2,
        )
        assert is_new1 is True
        assert is_new2 is False
        assert row2.last_seen_at.replace(tzinfo=UTC) == t2

    async def test_list_by_project_filter_severity(
        self, conn: asyncpg.Connection, rule_id: uuid.UUID
    ) -> None:
        repo = AlertFiredRepository(conn)
        now = datetime.now(UTC)
        await repo.upsert_or_update_last_seen(
            rule_id=rule_id,
            org_id=_ORG,
            project_id=_PROJ,
            dedup_key=f"sev-warn-{uuid.uuid4().hex}",
            severity="warn",
            current_value=75.0,
            triggered_reason="w",
            now=now,
        )
        await repo.upsert_or_update_last_seen(
            rule_id=rule_id,
            org_id=_ORG,
            project_id=_PROJ,
            dedup_key=f"sev-crit-{uuid.uuid4().hex}",
            severity="critical",
            current_value=95.0,
            triggered_reason="c",
            now=now,
        )
        warns = await repo.list_by_project(org_id=_ORG, project_id=_PROJ, severity="warn")
        crits = await repo.list_by_project(org_id=_ORG, project_id=_PROJ, severity="critical")
        assert all(r.severity == "warn" for r in warns)
        assert all(r.severity == "critical" for r in crits)

    async def test_get_last_fired(self, conn: asyncpg.Connection, rule_id: uuid.UUID) -> None:
        repo = AlertFiredRepository(conn)
        now = datetime.now(UTC)
        await repo.upsert_or_update_last_seen(
            rule_id=rule_id,
            org_id=_ORG,
            project_id=_PROJ,
            dedup_key=f"last-{uuid.uuid4().hex}",
            severity="warn",
            current_value=77.0,
            triggered_reason="last",
            now=now,
        )
        last = await repo.get_last_fired(rule_id=rule_id)
        assert last is not None
        assert last.rule_id == rule_id

    async def test_mark_escalated(self, conn: asyncpg.Connection, rule_id: uuid.UUID) -> None:
        repo = AlertFiredRepository(conn)
        now = datetime.now(UTC)
        row, _ = await repo.upsert_or_update_last_seen(
            rule_id=rule_id,
            org_id=_ORG,
            project_id=_PROJ,
            dedup_key=f"esc-{uuid.uuid4().hex}",
            severity="warn",
            current_value=75.0,
            triggered_reason="esc",
            now=now,
        )
        ok = await repo.mark_escalated(fired_id=row.fired_id, escalated_at=now)
        assert ok is True
        # idempotent: second call returns False (already escalated)
        ok2 = await repo.mark_escalated(fired_id=row.fired_id, escalated_at=now)
        assert ok2 is False
