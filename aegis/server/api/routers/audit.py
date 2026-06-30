"""Audit log read API (admin+ via VIEW_AUDIT_LOG)."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/audit-log", tags=["audit"])


@router.get("")
async def list_audit_log(
    org_id: uuid.UUID,
    action: str | None = Query(default=None, description="Filter by exact action"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_AUDIT_LOG)),
) -> list[dict[str, Any]]:
    """List audit entries for the org, newest first. admin+ (VIEW_AUDIT_LOG)."""
    params: list[Any] = [org_id]
    where = "org_id = $1"
    if action:
        params.append(action)
        where += f" AND action = ${len(params)}"
    params.extend([limit, offset])
    rows = await conn.fetch(
        f"SELECT id, org_id, actor_user_id, action, target_type, target_id, metadata,"
        f" created_at FROM audit_log WHERE {where}"
        f" ORDER BY created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}",
        *params,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["org_id"] = str(d["org_id"])
        d["actor_user_id"] = str(d["actor_user_id"]) if d["actor_user_id"] else None
        out.append(d)
    return out
