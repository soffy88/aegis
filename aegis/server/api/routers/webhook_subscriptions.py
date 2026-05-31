"""Webhook subscriptions router — C2-5."""

from __future__ import annotations

import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.repositories.webhook_delivery_repository import WebhookDeliveryQueueRepository
from aegis.server.repositories.webhook_subscription_repository import (
    WebhookSubscriptionRepository,
)
from aegis.server.schemas.webhook import (
    WebhookDeliveryResponse,
    WebhookSubscriptionCreate,
    WebhookSubscriptionResponse,
    WebhookSubscriptionUpdate,
)

router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/webhooks",
    tags=["webhooks"],
)


@router.post("", response_model=WebhookSubscriptionResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    org_id: uuid.UUID,
    data: WebhookSubscriptionCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> WebhookSubscriptionResponse:
    repo = WebhookSubscriptionRepository(conn)
    try:
        return await repo.create(org_id=org_id, created_by=user.user_id, data=data)
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(
                status.HTTP_409_CONFLICT, "webhook subscription name already exists"
            ) from exc
        raise


@router.get("", response_model=list[WebhookSubscriptionResponse])
async def list_webhooks(
    org_id: uuid.UUID,
    enabled_only: bool = Query(False),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_ORG)),
) -> list[WebhookSubscriptionResponse]:
    repo = WebhookSubscriptionRepository(conn)
    return await repo.list_by_org(org_id=org_id, enabled_only=enabled_only)


@router.get("/{sub_id}", response_model=WebhookSubscriptionResponse)
async def get_webhook(
    org_id: uuid.UUID,
    sub_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_ORG)),
) -> WebhookSubscriptionResponse:
    repo = WebhookSubscriptionRepository(conn)
    sub = await repo.get(sub_id=sub_id, org_id=org_id)
    if not sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook subscription not found")
    return sub


@router.patch("/{sub_id}", response_model=WebhookSubscriptionResponse)
async def update_webhook(
    org_id: uuid.UUID,
    sub_id: uuid.UUID,
    data: WebhookSubscriptionUpdate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> WebhookSubscriptionResponse:
    repo = WebhookSubscriptionRepository(conn)
    sub = await repo.update(sub_id=sub_id, org_id=org_id, data=data)
    if not sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook subscription not found")
    return sub


@router.delete("/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    org_id: uuid.UUID,
    sub_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> None:
    repo = WebhookSubscriptionRepository(conn)
    if not await repo.delete(sub_id=sub_id, org_id=org_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook subscription not found")


@router.post("/{sub_id}/test", response_model=dict)
async def send_test_event(
    org_id: uuid.UUID,
    sub_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict:
    """Enqueue a webhook.test event for manual verification. max_attempts=1 (no retry)."""
    sub_repo = WebhookSubscriptionRepository(conn)
    delivery_repo = WebhookDeliveryQueueRepository(conn)

    sub = await sub_repo.get(sub_id=sub_id, org_id=org_id)
    if not sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook subscription not found")

    await delivery_repo.enqueue(
        sub_id=sub.sub_id,
        org_id=org_id,
        event_type="webhook.test",
        payload={"test": True, "triggered_by": str(user.user_id)},
        max_attempts=1,
    )
    return {"status": "enqueued", "sub_id": str(sub_id)}


@router.get("/{sub_id}/deliveries", response_model=list[WebhookDeliveryResponse])
async def list_deliveries(
    org_id: uuid.UUID,
    sub_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_ORG)),
) -> list[WebhookDeliveryResponse]:
    sub_repo = WebhookSubscriptionRepository(conn)
    sub = await sub_repo.get(sub_id=sub_id, org_id=org_id)
    if not sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook subscription not found")
    delivery_repo = WebhookDeliveryQueueRepository(conn)
    return await delivery_repo.list_by_subscription(sub_id=sub_id, limit=limit)
