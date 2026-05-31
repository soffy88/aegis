"""Tests for WebhookDeliveryQueueRepository — C2-5. RUN_SMOKE=1."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from aegis.server.repositories.webhook_delivery_repository import WebhookDeliveryQueueRepository
from aegis.server.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)
from aegis.server.schemas.webhook import WebhookSubscriptionCreate

RUN_SMOKE = os.getenv("RUN_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN_SMOKE, reason="set RUN_SMOKE=1 to run")

_ORG = uuid.UUID("cc010001-0000-0000-0000-000000000000")
_USER = uuid.UUID("cc010003-0000-0000-0000-000000000000")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


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
        "INSERT INTO orgs (id, slug, name, plan) VALUES ($1, 'wdq-test', 'WDQ', 'free')"
        " ON CONFLICT DO NOTHING",
        _ORG,
    )
    await c.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1,'wdq@t.com','h')"
        " ON CONFLICT DO NOTHING",
        _USER,
    )
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def sub_id(conn: asyncpg.Connection) -> uuid.UUID:
    repo = WebhookSubscriptionRepository(conn)
    sub = await repo.create(
        org_id=_ORG,
        created_by=_USER,
        data=WebhookSubscriptionCreate(
            name=f"test-sub-{uuid.uuid4().hex[:8]}",
            url="https://example.com/wh",
            event_types=["alert.fired"],
        ),
    )
    return sub.sub_id


class TestWebhookDeliveryQueueRepository:
    async def test_enqueue(self, conn: asyncpg.Connection, sub_id: uuid.UUID) -> None:
        repo = WebhookDeliveryQueueRepository(conn)
        delivery = await repo.enqueue(
            sub_id=sub_id,
            org_id=_ORG,
            event_type="alert.fired",
            payload={"rule_id": "abc"},
            max_attempts=4,
        )
        assert delivery.state == "pending"
        assert delivery.attempt_no == 0
        assert delivery.event_type == "alert.fired"
        assert delivery.payload == {"rule_id": "abc"}

    async def test_claim_next_batch_skips_in_flight(
        self, conn: asyncpg.Connection, sub_id: uuid.UUID
    ) -> None:
        repo = WebhookDeliveryQueueRepository(conn)
        await repo.enqueue(
            sub_id=sub_id,
            org_id=_ORG,
            event_type="alert.fired",
            payload={"x": 1},
            max_attempts=3,
        )
        # First claim — should get 1
        batch1 = await repo.claim_next_batch(batch_size=10, now=_NOW)
        assert len(batch1) >= 1
        # Second claim with same now — already in_flight, skip locked
        batch2 = await repo.claim_next_batch(batch_size=10, now=_NOW)
        ids1 = {d.delivery_id for d in batch1}
        ids2 = {d.delivery_id for d in batch2}
        assert ids1.isdisjoint(ids2)

    async def test_mark_succeeded(self, conn: asyncpg.Connection, sub_id: uuid.UUID) -> None:
        repo = WebhookDeliveryQueueRepository(conn)
        delivery = await repo.enqueue(
            sub_id=sub_id,
            org_id=_ORG,
            event_type="alert.fired",
            payload={},
            max_attempts=4,
        )
        batch = await repo.claim_next_batch(batch_size=1, now=_NOW)
        claimed = next(d for d in batch if d.delivery_id == delivery.delivery_id)
        await repo.mark_succeeded(delivery_id=claimed.delivery_id, status_code=200, now=_NOW)
        rows = await repo.list_by_subscription(sub_id=sub_id, org_id=_ORG)
        updated = next(r for r in rows if r.delivery_id == delivery.delivery_id)
        assert updated.state == "succeeded"
        assert updated.last_status_code == 200

    async def test_mark_failed_for_retry_increments_attempt(
        self, conn: asyncpg.Connection, sub_id: uuid.UUID
    ) -> None:
        repo = WebhookDeliveryQueueRepository(conn)
        delivery = await repo.enqueue(
            sub_id=sub_id,
            org_id=_ORG,
            event_type="alert.fired",
            payload={},
            max_attempts=4,
        )
        batch = await repo.claim_next_batch(batch_size=1, now=_NOW)
        claimed = next(d for d in batch if d.delivery_id == delivery.delivery_id)
        await repo.mark_failed_for_retry(
            delivery_id=claimed.delivery_id,
            status_code=503,
            error="upstream down",
            backoff_seconds=30,
            now=_NOW,
        )
        rows = await repo.list_by_subscription(sub_id=sub_id, org_id=_ORG)
        updated = next(r for r in rows if r.delivery_id == delivery.delivery_id)
        assert updated.state == "pending"
        assert updated.attempt_no == 1
        assert updated.next_attempt_at > _NOW

    async def test_mark_dead_letter(self, conn: asyncpg.Connection, sub_id: uuid.UUID) -> None:
        repo = WebhookDeliveryQueueRepository(conn)
        delivery = await repo.enqueue(
            sub_id=sub_id,
            org_id=_ORG,
            event_type="alert.fired",
            payload={},
            max_attempts=1,
        )
        batch = await repo.claim_next_batch(batch_size=1, now=_NOW)
        claimed = next(d for d in batch if d.delivery_id == delivery.delivery_id)
        await repo.mark_dead_letter(
            delivery_id=claimed.delivery_id, status_code=400, error="bad request"
        )
        rows = await repo.list_by_subscription(sub_id=sub_id, org_id=_ORG)
        updated = next(r for r in rows if r.delivery_id == delivery.delivery_id)
        assert updated.state == "dead_letter"
        assert updated.last_error == "bad request"
