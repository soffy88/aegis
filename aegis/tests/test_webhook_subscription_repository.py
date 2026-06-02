"""Tests for WebhookSubscriptionRepository — C2-5. RUN_SMOKE=1."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from aegis.server.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)
from aegis.server.schemas.webhook import WebhookSubscriptionCreate, WebhookSubscriptionUpdate

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_ORG = uuid.UUID("bb010001-0000-0000-0000-000000000000")
_USER = uuid.UUID("bb010003-0000-0000-0000-000000000000")
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
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'wh-test', 'WH', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG,
    )
    await c.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1,'wh@t.com','h')"
        " ON CONFLICT DO NOTHING",
        _USER,
    )
    try:
        yield c
    finally:
        await c.close()


def _sub(name: str = "my-hook", **kwargs: Any) -> WebhookSubscriptionCreate:
    return WebhookSubscriptionCreate(
        name=name,
        url="https://example.com/webhook",
        event_types=["alert.fired"],
        **kwargs,
    )


class TestWebhookSubscriptionRepository:
    async def test_create(self, conn: asyncpg.Connection) -> None:
        repo = WebhookSubscriptionRepository(conn)
        sub = await repo.create(org_id=_ORG, created_by=_USER, data=_sub())
        assert sub.name == "my-hook"
        assert sub.event_types == ["alert.fired"]
        assert sub.enabled is True
        assert sub.retry_count == 3

    async def test_get_by_id(self, conn: asyncpg.Connection) -> None:
        repo = WebhookSubscriptionRepository(conn)
        created = await repo.create(org_id=_ORG, created_by=_USER, data=_sub("get-test"))
        found = await repo.get(sub_id=created.sub_id, org_id=_ORG)
        assert found is not None
        assert found.sub_id == created.sub_id

    async def test_get_wrong_org_returns_none(self, conn: asyncpg.Connection) -> None:
        repo = WebhookSubscriptionRepository(conn)
        created = await repo.create(org_id=_ORG, created_by=_USER, data=_sub("org-test"))
        assert await repo.get(sub_id=created.sub_id, org_id=uuid.uuid4()) is None

    async def test_list_by_org_enabled_only(self, conn: asyncpg.Connection) -> None:
        repo = WebhookSubscriptionRepository(conn)
        await repo.create(org_id=_ORG, created_by=_USER, data=_sub("list-en"))
        await repo.create(org_id=_ORG, created_by=_USER, data=_sub("list-dis", enabled=False))
        all_subs = await repo.list_by_org(org_id=_ORG)
        enabled = await repo.list_by_org(org_id=_ORG, enabled_only=True)
        assert len(enabled) < len(all_subs)
        assert all(s.enabled for s in enabled)

    async def test_update_partial(self, conn: asyncpg.Connection) -> None:
        repo = WebhookSubscriptionRepository(conn)
        created = await repo.create(org_id=_ORG, created_by=_USER, data=_sub("upd-test"))
        updated = await repo.update(
            sub_id=created.sub_id,
            org_id=_ORG,
            data=WebhookSubscriptionUpdate(retry_count=5),
        )
        assert updated is not None
        assert updated.retry_count == 5
        assert updated.url == "https://example.com/webhook"

    async def test_delete(self, conn: asyncpg.Connection) -> None:
        repo = WebhookSubscriptionRepository(conn)
        created = await repo.create(org_id=_ORG, created_by=_USER, data=_sub("del-test"))
        assert await repo.delete(sub_id=created.sub_id, org_id=_ORG) is True
        assert await repo.get(sub_id=created.sub_id, org_id=_ORG) is None

    async def test_delete_nonexistent_returns_false(self, conn: asyncpg.Connection) -> None:
        repo = WebhookSubscriptionRepository(conn)
        assert await repo.delete(sub_id=uuid.uuid4(), org_id=_ORG) is False

    async def test_unique_name_per_org(self, conn: asyncpg.Connection) -> None:
        repo = WebhookSubscriptionRepository(conn)
        await repo.create(org_id=_ORG, created_by=_USER, data=_sub("dup-name"))
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo.create(org_id=_ORG, created_by=_USER, data=_sub("dup-name"))
