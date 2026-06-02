"""Smoke tests for ErrorIssueRepository — RUN_SMOKE=1."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import asyncpg
import pytest

from aegis.server.repositories.error_issue_repository import ErrorIssueRepository

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_ORG = uuid.UUID("ee020001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("ee020002-0000-0000-0000-000000000000")


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
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'ei-test', 'EI', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG,
    )
    await c.execute(
        "INSERT INTO projects (id, org_id, name, slug, display_name)"
        " VALUES ($1,$2,'ei-proj','ei-proj','EI Proj') ON CONFLICT DO NOTHING",
        _PROJ,
        _ORG,
    )
    try:
        yield c
    finally:
        await c.close()


class TestErrorIssueRepository:
    async def test_upsert_new(self, conn: asyncpg.Connection) -> None:
        repo = ErrorIssueRepository(conn)
        fp = f"fp-{uuid.uuid4().hex[:8]}"
        issue, is_new = await repo.upsert_by_fingerprint(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint=fp,
            exception_type="TypeError",
            exception_value="bad value",
        )
        assert is_new is True
        assert issue.event_count == 1
        assert issue.state == "unresolved"
        assert issue.fingerprint == fp

    async def test_upsert_existing_increments_count(self, conn: asyncpg.Connection) -> None:
        repo = ErrorIssueRepository(conn)
        fp = f"fp-{uuid.uuid4().hex[:8]}"
        issue1, is_new1 = await repo.upsert_by_fingerprint(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint=fp,
            exception_type="ValueError",
            exception_value="first",
        )
        issue2, is_new2 = await repo.upsert_by_fingerprint(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint=fp,
            exception_type="ValueError",
            exception_value="second",
        )
        assert is_new1 is True
        assert is_new2 is False
        assert issue2.event_count == 2
        assert issue2.issue_id == issue1.issue_id

    async def test_get_by_id(self, conn: asyncpg.Connection) -> None:
        repo = ErrorIssueRepository(conn)
        fp = f"fp-{uuid.uuid4().hex[:8]}"
        created, _ = await repo.upsert_by_fingerprint(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint=fp,
            exception_type="RuntimeError",
            exception_value="oops",
        )
        fetched = await repo.get(issue_id=created.issue_id, org_id=_ORG)
        assert fetched is not None
        assert fetched.issue_id == created.issue_id

    async def test_get_wrong_org_returns_none(self, conn: asyncpg.Connection) -> None:
        repo = ErrorIssueRepository(conn)
        fp = f"fp-{uuid.uuid4().hex[:8]}"
        created, _ = await repo.upsert_by_fingerprint(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint=fp,
            exception_type="OSError",
            exception_value="file not found",
        )
        other_org = uuid.uuid4()
        result = await repo.get(issue_id=created.issue_id, org_id=other_org)
        assert result is None

    async def test_list_by_project_filter_state(self, conn: asyncpg.Connection) -> None:
        repo = ErrorIssueRepository(conn)
        fp_u = f"fp-unres-{uuid.uuid4().hex[:8]}"
        fp_r = f"fp-res-{uuid.uuid4().hex[:8]}"
        issue_u, _ = await repo.upsert_by_fingerprint(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint=fp_u,
            exception_type="KeyError",
            exception_value="missing key",
        )
        issue_r, _ = await repo.upsert_by_fingerprint(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint=fp_r,
            exception_type="IndexError",
            exception_value="out of range",
        )
        await repo.mark_resolved(issue_id=issue_r.issue_id, org_id=_ORG)

        unresolved = await repo.list_by_project(org_id=_ORG, project_id=_PROJ, state="unresolved")
        ids = {i.issue_id for i in unresolved}
        assert issue_u.issue_id in ids
        assert issue_r.issue_id not in ids

    async def test_mark_resolved(self, conn: asyncpg.Connection) -> None:
        repo = ErrorIssueRepository(conn)
        fp = f"fp-{uuid.uuid4().hex[:8]}"
        issue, _ = await repo.upsert_by_fingerprint(
            org_id=_ORG,
            project_id=_PROJ,
            fingerprint=fp,
            exception_type="TimeoutError",
            exception_value="timed out",
        )
        ok = await repo.mark_resolved(issue_id=issue.issue_id, org_id=_ORG)
        assert ok is True
        fetched = await repo.get(issue_id=issue.issue_id, org_id=_ORG)
        assert fetched is not None
        assert fetched.state == "resolved"
