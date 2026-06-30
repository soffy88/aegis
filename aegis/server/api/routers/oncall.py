"""On-call schedule API (org-scoped). admin manages; viewer sees who's on call."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/oncall", tags=["oncall"])


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rotation: list[uuid.UUID] = Field(min_length=1)
    shift_length_seconds: int = Field(default=604800, ge=300)
    anchor_at: datetime | None = None
    enabled: bool = True


def _row(r: asyncpg.Record) -> dict[str, Any]:
    d = dict(r)
    d["id"] = str(d["id"])
    d["org_id"] = str(d["org_id"])
    d["rotation"] = [str(u) for u in d["rotation"]]
    return d


@router.get("/schedules")
async def list_schedules(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT * FROM oncall_schedules WHERE org_id = $1 ORDER BY created_at", org_id
    )
    return [_row(r) for r in rows]


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
async def create_schedule(
    org_id: uuid.UUID,
    body: ScheduleCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
) -> dict[str, Any]:
    try:
        row = await conn.fetchrow(
            "INSERT INTO oncall_schedules"
            " (org_id, name, rotation, shift_length_seconds, anchor_at, enabled)"
            " VALUES ($1,$2,$3,$4,COALESCE($5, now()),$6) RETURNING *",
            org_id,
            body.name,
            body.rotation,
            body.shift_length_seconds,
            body.anchor_at,
            body.enabled,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"schedule {body.name!r} already exists"
        ) from exc
    return _row(row)


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
) -> None:
    result = await conn.execute(
        "DELETE FROM oncall_schedules WHERE org_id=$1 AND id=$2", org_id, schedule_id
    )
    if result == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "schedule not found")


@router.get("/current")
async def get_current_oncall(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> dict[str, Any]:
    """Who is on call right now (first enabled schedule). viewer+."""
    from aegis.server.services.oncall import current_oncall

    uid = await current_oncall(conn, org_id=org_id)
    return {"oncall_user_id": str(uid) if uid else None}
