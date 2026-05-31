"""Webhook subscription CRUD repository — C2-5."""

from __future__ import annotations

import uuid

import asyncpg

from aegis.server.schemas.webhook import (
    WebhookSubscriptionCreate,
    WebhookSubscriptionResponse,
    WebhookSubscriptionUpdate,
)


class WebhookSubscriptionRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(
        self,
        *,
        org_id: uuid.UUID,
        created_by: uuid.UUID,
        data: WebhookSubscriptionCreate,
    ) -> WebhookSubscriptionResponse:
        row = await self.conn.fetchrow(
            """
            INSERT INTO webhook_subscriptions (
                org_id, name, url, secret_encrypted, event_types,
                retry_count, retry_backoff_seconds, enabled, created_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING *
            """,
            org_id,
            data.name,
            data.url,
            data.secret_encrypted,
            data.event_types,
            data.retry_count,
            data.retry_backoff_seconds,
            data.enabled,
            created_by,
        )
        return WebhookSubscriptionResponse.model_validate(dict(row))

    async def get(
        self,
        *,
        sub_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> WebhookSubscriptionResponse | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM webhook_subscriptions WHERE sub_id=$1 AND org_id=$2",
            sub_id,
            org_id,
        )
        return WebhookSubscriptionResponse.model_validate(dict(row)) if row else None

    async def list_by_org(
        self,
        *,
        org_id: uuid.UUID,
        enabled_only: bool = False,
    ) -> list[WebhookSubscriptionResponse]:
        query = "SELECT * FROM webhook_subscriptions WHERE org_id=$1"
        if enabled_only:
            query += " AND enabled = TRUE"
        query += " ORDER BY created_at DESC"
        rows = await self.conn.fetch(query, org_id)
        return [WebhookSubscriptionResponse.model_validate(dict(r)) for r in rows]

    async def update(
        self,
        *,
        sub_id: uuid.UUID,
        org_id: uuid.UUID,
        data: WebhookSubscriptionUpdate,
    ) -> WebhookSubscriptionResponse | None:
        updates = data.model_dump(exclude_unset=True, exclude_none=True)
        if not updates:
            return await self.get(sub_id=sub_id, org_id=org_id)
        set_clauses = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(updates.keys()))
        query = (
            f"UPDATE webhook_subscriptions SET {set_clauses}, updated_at = NOW()"
            " WHERE sub_id=$1 AND org_id=$2 RETURNING *"
        )
        row = await self.conn.fetchrow(query, sub_id, org_id, *updates.values())
        return WebhookSubscriptionResponse.model_validate(dict(row)) if row else None

    async def delete(
        self,
        *,
        sub_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> bool:
        result = await self.conn.execute(
            "DELETE FROM webhook_subscriptions WHERE sub_id=$1 AND org_id=$2",
            sub_id,
            org_id,
        )
        return result == "DELETE 1"
