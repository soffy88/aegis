"""Status components — named service components with health (operational/degraded/
outage), complementing the incident-only public status page."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/status-components", tags=["status-components"])
_STATES = {"operational", "degraded", "partial_outage", "major_outage", "maintenance"}


class ComponentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    status: str = "operational"


@router.get("")
async def list_components(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    comps = await conn.fetch(
        "SELECT id, name, status FROM aegis_status_components WHERE org_id=$1 ORDER BY name", org_id
    )
    order = ["operational", "maintenance", "degraded", "partial_outage", "major_outage"]
    worst = "operational"
    for c in comps:
        if c["status"] in order and order.index(c["status"]) > order.index(worst):
            worst = c["status"]
    return {"overall": worst, "components": [dict(c) | {"id": str(c["id"])} for c in comps]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def upsert_component(
    org_id: uuid.UUID,
    req: ComponentRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict[str, str]:
    if req.status not in _STATES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"status must be one of {sorted(_STATES)}")
    cid = await conn.fetchval(
        "INSERT INTO aegis_status_components (org_id, name, status) VALUES ($1,$2,$3)"
        " ON CONFLICT (org_id, name) DO UPDATE SET status=EXCLUDED.status RETURNING id",
        org_id, req.name, req.status,
    )
    return {"id": str(cid)}


@router.delete("/{comp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def del_component(
    org_id: uuid.UUID,
    comp_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> None:
    await conn.execute("DELETE FROM aegis_status_components WHERE id=$1 AND org_id=$2", comp_id, org_id)
