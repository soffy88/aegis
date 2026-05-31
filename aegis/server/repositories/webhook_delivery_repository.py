"""Webhook delivery queue repository — C2-5."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from aegis.server.schemas.webhook import WebhookDeliveryResponse


class WebhookDeliveryQueueRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def enqueue(
        self,
        *,
        sub_id: uuid.UUID,
        org_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any],
        max_attempts: int,
    ) -> WebhookDeliveryResponse:
        row = await self.conn.fetchrow(
            """
            INSERT INTO webhook_delivery_queue (
                sub_id, org_id, event_type, payload, max_attempts
            ) VALUES ($1, $2, $3, $4::jsonb, $5)
            RETURNING *
            """,
            sub_id,
            org_id,
            event_type,
            json.dumps(payload),
            max_attempts,
        )
        return WebhookDeliveryResponse.model_validate(dict(row))

    async def claim_next_batch(
        self,
        *,
        batch_size: int = 10,
        now: datetime | None = None,
    ) -> list[WebhookDeliveryResponse]:
        """Atomically claim pending deliveries due now; marks them in_flight."""
        now = now or datetime.now(UTC)
        rows = await self.conn.fetch(
            """
            UPDATE webhook_delivery_queue
            SET state = 'in_flight', last_attempt_at = $1
            WHERE delivery_id IN (
                SELECT delivery_id FROM webhook_delivery_queue
                WHERE state = 'pending' AND next_attempt_at <= $1
                ORDER BY next_attempt_at ASC
                LIMIT $2
                FOR UPDATE SKIP LOCKED
            )
            RETURNING *
            """,
            now,
            batch_size,
        )
        return [WebhookDeliveryResponse.model_validate(dict(r)) for r in rows]

    async def mark_succeeded(
        self,
        *,
        delivery_id: uuid.UUID,
        status_code: int,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        await self.conn.execute(
            """
            UPDATE webhook_delivery_queue
            SET state = 'succeeded', last_status_code = $2, succeeded_at = $3, last_error = NULL
            WHERE delivery_id = $1
            """,
            delivery_id,
            status_code,
            now,
        )

    async def mark_failed_for_retry(
        self,
        *,
        delivery_id: uuid.UUID,
        status_code: int | None,
        error: str,
        backoff_seconds: int,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        next_at = now + timedelta(seconds=backoff_seconds)
        await self.conn.execute(
            """
            UPDATE webhook_delivery_queue
            SET state = 'pending', attempt_no = attempt_no + 1,
                last_status_code = $2, last_error = $3, next_attempt_at = $4
            WHERE delivery_id = $1
            """,
            delivery_id,
            status_code,
            error,
            next_at,
        )

    async def mark_dead_letter(
        self,
        *,
        delivery_id: uuid.UUID,
        status_code: int | None,
        error: str,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE webhook_delivery_queue
            SET state = 'dead_letter', last_status_code = $2, last_error = $3
            WHERE delivery_id = $1
            """,
            delivery_id,
            status_code,
            error,
        )

    async def list_by_subscription(
        self,
        *,
        sub_id: uuid.UUID,
        limit: int = 50,
    ) -> list[WebhookDeliveryResponse]:
        rows = await self.conn.fetch(
            """
            SELECT * FROM webhook_delivery_queue
            WHERE sub_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            sub_id,
            limit,
        )
        return [WebhookDeliveryResponse.model_validate(dict(r)) for r in rows]
