"""Tests for ReleaseGateRepository — C2-4a. RUN_SMOKE=1."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest

from aegis.server.repositories.release_gate_repository import ReleaseGateRepository

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_ORG = uuid.UUID("aaaa0001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("aaaa0002-0000-0000-0000-000000000000")
_USER = uuid.UUID("aaaa0003-0000-0000-0000-000000000000")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


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
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'rg-test', 'RG', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG,
    )
    await c.execute(
        "INSERT INTO projects (id, org_id, name, slug, display_name)"
        " VALUES ($1,$2,'RGP','rgp','RGP') ON CONFLICT DO NOTHING",
        _PROJ,
        _ORG,
    )
    await c.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1,'rg@t.com','h')"
        " ON CONFLICT DO NOTHING",
        _USER,
    )
    try:
        yield c
    finally:
        await c.close()


def _make_repo(conn: asyncpg.Connection) -> ReleaseGateRepository:
    return ReleaseGateRepository(conn)


class TestReleaseGateRepository:
    async def test_create(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        gate = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="restart_container",
            action_payload={"container": "nginx"},
            autoheal_event_id=None,
            expires_in_hours=24,
            now=_NOW,
        )
        assert gate.state == "pending"
        assert gate.action_kind == "restart_container"
        assert gate.action_payload == {"container": "nginx"}
        assert gate.decided_by is None
        assert gate.decided_at is None

    async def test_get_existing(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        created = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="delete_volume",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=24,
            now=_NOW,
        )
        found = await repo.get(gate_id=created.gate_id, org_id=_ORG, now=_NOW)
        assert found is not None
        assert found.gate_id == created.gate_id
        assert found.state == "pending"

    async def test_get_lazy_expire_pending_past_expiry(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        past = _NOW - timedelta(hours=25)
        gate = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="force_rebuild",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=1,
            now=past,
        )
        # now > expires_at → lazy expire should mark it expired
        fetched = await repo.get(gate_id=gate.gate_id, org_id=_ORG, now=_NOW)
        assert fetched is not None
        assert fetched.state == "expired"

    async def test_get_lazy_expire_does_not_affect_decided(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        past = _NOW - timedelta(hours=25)
        gate = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="restart_container",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=48,
            now=past,
        )
        # decide it (still within window from past perspective)
        await repo.decide(
            gate_id=gate.gate_id,
            org_id=_ORG,
            decided_by=_USER,
            decision="approved",
            decision_reason="ok",
            now=past + timedelta(hours=1),
        )
        # lazy expire should NOT change state=approved to expired
        fetched = await repo.get(gate_id=gate.gate_id, org_id=_ORG, now=_NOW)
        assert fetched is not None
        assert fetched.state == "approved"

    async def test_list_by_project_filter_state(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        # create two pending + approve one
        g1 = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="restart_container",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=24,
            now=_NOW,
        )
        await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="delete_volume",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=24,
            now=_NOW,
        )
        await repo.decide(
            gate_id=g1.gate_id,
            org_id=_ORG,
            decided_by=_USER,
            decision="approved",
            decision_reason="ok",
            now=_NOW,
        )
        approved = await repo.list_by_project(
            org_id=_ORG, project_id=_PROJ, state="approved", now=_NOW
        )
        assert all(g.state == "approved" for g in approved)

    async def test_decide_approved_success(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        gate = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="restart_container",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=24,
            now=_NOW,
        )
        result = await repo.decide(
            gate_id=gate.gate_id,
            org_id=_ORG,
            decided_by=_USER,
            decision="approved",
            decision_reason="looks safe",
            now=_NOW,
        )
        assert result is not None
        assert result.state == "approved"
        assert result.decided_by == _USER
        assert result.decision_reason == "looks safe"

    async def test_decide_rejected_success(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        gate = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="delete_volume",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=24,
            now=_NOW,
        )
        result = await repo.decide(
            gate_id=gate.gate_id,
            org_id=_ORG,
            decided_by=_USER,
            decision="rejected",
            decision_reason="too risky",
            now=_NOW,
        )
        assert result is not None
        assert result.state == "rejected"

    async def test_decide_after_expiry_returns_none(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        past = _NOW - timedelta(hours=25)
        gate = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="restart_container",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=1,
            now=past,
        )
        # Try to decide after expiry
        result = await repo.decide(
            gate_id=gate.gate_id,
            org_id=_ORG,
            decided_by=_USER,
            decision="approved",
            decision_reason="too late",
            now=_NOW,
        )
        assert result is None

    async def test_decide_after_already_decided_returns_none(
        self, conn: asyncpg.Connection
    ) -> None:
        repo = _make_repo(conn)
        gate = await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="restart_container",
            action_payload={},
            autoheal_event_id=None,
            expires_in_hours=24,
            now=_NOW,
        )
        await repo.decide(
            gate_id=gate.gate_id,
            org_id=_ORG,
            decided_by=_USER,
            decision="approved",
            decision_reason="first",
            now=_NOW,
        )
        result = await repo.decide(
            gate_id=gate.gate_id,
            org_id=_ORG,
            decided_by=_USER,
            decision="rejected",
            decision_reason="second",
            now=_NOW,
        )
        assert result is None

    async def test_unique_autoheal_event_id_conflict(self, conn: asyncpg.Connection) -> None:
        repo = _make_repo(conn)
        event_id = uuid.uuid4()
        await repo.create(
            org_id=_ORG,
            project_id=_PROJ,
            requested_by=_USER,
            action_kind="restart_container",
            action_payload={},
            autoheal_event_id=event_id,
            expires_in_hours=24,
            now=_NOW,
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.create(
                org_id=_ORG,
                project_id=_PROJ,
                requested_by=_USER,
                action_kind="restart_container",
                action_payload={},
                autoheal_event_id=event_id,
                expires_in_hours=24,
                now=_NOW,
            )
