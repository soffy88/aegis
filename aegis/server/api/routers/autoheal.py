"""AutoHeal monitoring and control API."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_min_role, require_permission
from aegis.server.models.membership import Role
from aegis.server.repositories.autoheal_event_repository import AutoHealEventRepository
from aegis.server.services.platform_flags import AUTOHEAL_KILL_SWITCH, get_flag, set_flag

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/autoheal", tags=["autoheal"])


class KillSwitchUpdate(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=500)


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


@router.get("/kill-switch")
async def get_kill_switch(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """§5.3 全局自愈急停开关当前状态。缺行=未急停(默认放行)。viewer+ 可读。"""
    return await get_flag(conn, AUTOHEAL_KILL_SWITCH)


@router.put("/kill-switch")
async def put_kill_switch(
    org_id: uuid.UUID,
    body: KillSwitchUpdate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_min_role(Role.ADMIN)),
) -> dict[str, Any]:
    """§5.3 置位/解除全局自愈急停(DB 支撑,事中即生效不必重启)。admin+ 方可翻动
    ——这是全平台范围的动作,故门槛高于常规 autoheal 操作(TRIGGER_AUTOHEAL/operator+)。"""
    await set_flag(conn, AUTOHEAL_KILL_SWITCH, enabled=body.enabled, reason=body.reason)
    log.warning(
        "autoheal_kill_switch_changed enabled=%s reason=%s by=%s org_id=%s",
        body.enabled,
        body.reason,
        user.user_id,
        org_id,
    )
    return await get_flag(conn, AUTOHEAL_KILL_SWITCH)
