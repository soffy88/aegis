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
from aegis.server.repositories.autoheal_event_repository import AutoHealEventRepository

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/autoheal", tags=["autoheal"])


@router.get("/events")
async def list_autoheal_events(
    org_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=1000),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """Get recent autoheal alert events."""
    return await AutoHealEventRepository(conn).list_for_org(org_id=org_id, limit=limit)


@router.get("/events/{event_id}")
async def get_autoheal_event(
    org_id: uuid.UUID,
    event_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get single alert event detail."""
    row = await AutoHealEventRepository(conn).get(org_id=org_id, event_id=event_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    return row


@router.post("/events/{event_id}/retry")
async def retry_autoheal(
    org_id: uuid.UUID,
    event_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Acknowledge an autoheal event and mark it handled.

    This records operator handling of the signal. Automatic remediation execution
    (matching the signal to a plugin/action_plan and running AutoHealEngine) is
    gated on an autoheal policy model that is not yet configured — see STATUS.md
    Needs-Human. Until then this endpoint performs the real, safe state change
    (mark handled) rather than the previous no-op TODO.
    """
    repo = AutoHealEventRepository(conn)
    event = await repo.get(org_id=org_id, event_id=event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")

    await repo.mark_handled(org_id=org_id, event_id=event_id)
    log.info("autoheal_event_handled event_id=%s org_id=%s by=%s", event_id, org_id, user.user_id)
    return {
        "message": "event marked handled",
        "event_id": str(event_id),
        "handled": True,
        "auto_remediation": "not_configured",
    }


@router.get("/stats")
async def get_autoheal_stats(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get autoheal summary stats."""
    return await AutoHealEventRepository(conn).stats(org_id=org_id)
