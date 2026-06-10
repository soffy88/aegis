"""AutoHeal monitoring and control API."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/autoheal", tags=["autoheal"])


async def autoheal_cycle(event_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Skeleton for autoheal_cycle (Batch B logic).

    In a real implementation, this would:
    1. Fetch event from DB
    2. Match with AutoHealEngine
    3. Execute lifecycle
    4. Update handled=true
    """
    log.info("autoheal_cycle_triggered event_id=%s org_id=%s", event_id, org_id)
    # TODO: real implementation integration


@router.get("/events")
async def list_autoheal_events(
    org_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=1000),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """Get recent autoheal alert events."""
    rows = await conn.fetch(
        "SELECT * FROM aegis_alert_events WHERE org_id = $1 ORDER BY created_at DESC LIMIT $2",
        org_id,
        limit,
    )
    return [dict(r) for r in rows]


@router.get("/events/{event_id}")
async def get_autoheal_event(
    org_id: uuid.UUID,
    event_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get single alert event detail."""
    row = await conn.fetchrow(
        "SELECT * FROM aegis_alert_events WHERE org_id = $1 AND id = $2",
        org_id,
        event_id,
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    return dict(row)


@router.post("/events/{event_id}/retry")
async def retry_autoheal(
    org_id: uuid.UUID,
    event_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, str]:
    """Manually re-trigger autoheal for an event."""
    # Note: real implementation should check if already handled or in progress
    await autoheal_cycle(event_id, org_id)
    return {"message": "autoheal cycle triggered"}


@router.get("/stats")
async def get_autoheal_stats(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get autoheal summary stats."""
    sql = """
    SELECT
        COUNT(*) FILTER (WHERE created_at > now() - interval '24h') AS today_total,
        COUNT(*) FILTER (
            WHERE handled = true AND created_at > now() - interval '24h'
        ) AS today_handled,
        COUNT(*) FILTER (WHERE handled = false AND severity = 'critical') AS pending_critical,
        COUNT(*) FILTER (WHERE handled = false) AS pending_total
    FROM aegis_alert_events
    WHERE org_id = $1
    """
    row = await conn.fetchrow(sql, org_id)
    if not row:
        return {
            "today_total": 0,
            "today_handled": 0,
            "pending_critical": 0,
            "pending_total": 0,
        }
    return dict(row)
