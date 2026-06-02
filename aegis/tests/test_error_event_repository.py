"""Smoke tests for ErrorEventRepository — RUN_SMOKE=1."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from aegis.server.repositories.error_event_repository import ErrorEventRepository
from aegis.server.schemas.error_monitoring import ErrorEventCreate

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_ORG = uuid.UUID("ee010001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("ee010002-0000-0000-0000-000000000000")


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
    await c.execute(
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'ee-test', 'EE', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG,
    )
    await c.execute(
        "INSERT INTO projects (id, org_id, name, slug, display_name)"
        " VALUES ($1,$2,'ee-proj','ee-proj','EE Proj') ON CONFLICT DO NOTHING",
        _PROJ,
        _ORG,
    )
    try:
        yield c
    finally:
        await c.close()


def _make_create(**kwargs: Any) -> ErrorEventCreate:
    defaults = dict(
        org_id=_ORG,
        project_id=_PROJ,
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        exception_type="TypeError",
        exception_value="bad input",
        level="error",
    )
    defaults.update(kwargs)
    return ErrorEventCreate(**defaults)


class TestErrorEventRepository:
    async def test_insert(self, conn: asyncpg.Connection) -> None:
        repo = ErrorEventRepository(conn)
        ev = await repo.insert(data=_make_create())
        assert ev.event_id is not None
        assert ev.issue_id is None
        assert ev.exception_type == "TypeError"
        assert ev.level == "error"

    async def test_set_issue_id_success(self, conn: asyncpg.Connection) -> None:
        repo = ErrorEventRepository(conn)
        ev = await repo.insert(data=_make_create())
        issue_id = uuid.uuid4()
        ok = await repo.set_issue_id(event_id=ev.event_id, issue_id=issue_id)
        assert ok is True

    async def test_set_issue_id_not_found(self, conn: asyncpg.Connection) -> None:
        repo = ErrorEventRepository(conn)
        ok = await repo.set_issue_id(event_id=uuid.uuid4(), issue_id=uuid.uuid4())
        assert ok is False

    async def test_list_by_issue(self, conn: asyncpg.Connection) -> None:
        repo = ErrorEventRepository(conn)
        issue_id = uuid.uuid4()
        ev1 = await repo.insert(data=_make_create(fingerprint="fp-issue-1"))
        ev2 = await repo.insert(data=_make_create(fingerprint="fp-issue-2"))
        await repo.set_issue_id(event_id=ev1.event_id, issue_id=issue_id)
        await repo.set_issue_id(event_id=ev2.event_id, issue_id=issue_id)
        rows = await repo.list_by_issue(issue_id=issue_id)
        ids = {r.event_id for r in rows}
        assert ev1.event_id in ids
        assert ev2.event_id in ids

    async def test_list_by_project_with_since_filter(self, conn: asyncpg.Connection) -> None:
        repo = ErrorEventRepository(conn)
        # Insert one old and one recent event
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        await repo.insert(data=_make_create(fingerprint="fp-old", ts=old_ts))
        await repo.insert(data=_make_create(fingerprint="fp-new"))

        cutoff = datetime(2025, 1, 1, tzinfo=UTC)
        rows = await repo.list_by_project(org_id=_ORG, project_id=_PROJ, since=cutoff)
        fps = {r.fingerprint for r in rows}
        assert "fp-old" not in fps
        assert "fp-new" in fps
