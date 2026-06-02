"""Real-container smoke tests — require RUN_SMOKE=1 and Docker socket.

Run with:
    RUN_SMOKE=1 pytest aegis/tests/smoke/ -v

These tests spin up a real Postgres container via testcontainers and exercise
the migration runner + event_trail read/write path against a live database.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any

import asyncpg
import pytest

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")


@pytest.fixture(scope="module")
def pg_container() -> Generator[Any, None, None]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("timescale/timescaledb:2.26.3-pg18") as pg:
        yield pg


@pytest.fixture
async def pg_conn(pg_container: Any) -> AsyncGenerator[asyncpg.Connection, None]:
    dsn = pg_container.get_connection_url(driver=None)
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_migrations_apply_on_real_postgres(pg_conn: asyncpg.Connection) -> None:
    """All 4 migrations apply on a fresh Postgres container."""
    from aegis.server.persistence.migrations import apply_migrations

    n = await apply_migrations(pg_conn)
    assert n >= 4, f"Expected ≥4 migrations on first run, got {n}"


async def test_migrations_idempotent(pg_conn: asyncpg.Connection) -> None:
    """Re-running apply_migrations returns 0 (already applied)."""
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(pg_conn)
    n2 = await apply_migrations(pg_conn)
    assert n2 == 0, f"Expected 0 on second run, got {n2}"


async def test_event_trail_write_then_read(pg_conn: asyncpg.Connection) -> None:
    """write an event → recent_events returns it with correct event_type."""
    from aegis.server.persistence.event_trail import append_event, recent_events
    from aegis.server.persistence.migrations import apply_migrations

    await apply_migrations(pg_conn)

    org_id = uuid.uuid4()
    project_id = uuid.uuid4()

    event_id = await append_event(
        conn=pg_conn,
        org_id=org_id,
        project_id=project_id,
        event_type="smoke_test_event",
        payload={"smoke": True},
    )
    assert isinstance(event_id, uuid.UUID)

    events = await recent_events(conn=pg_conn, org_id=org_id, project_id=project_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "smoke_test_event"
