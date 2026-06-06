"""event_trail HTTP API."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence import (
    append_event,
    causal_chain,
    recent_events,
)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/events", tags=["events"])


class EventCreate(BaseModel):
    event_type: str
    severity: str = "info"
    payload: dict[str, Any] = Field(default_factory=dict)
    service: str | None = None
    resource: str | None = None
    environment: str = "prod"
    trace_id: str | None = None
    initiated_by: str = "user"


class EventRead(BaseModel):
    id: uuid.UUID
    ts: Any  # datetime serialized
    event_type: str
    severity: str | None
    payload: dict[str, Any]
    omodul_kind: str | None = None
    autoheal_plugin: str | None = None
    trace_id: str | None = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_event(
    org_id: uuid.UUID,
    body: EventCreate,
    project_id: uuid.UUID = Query(..., description="Project to attach this event to"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> dict[str, Any]:
    """Append a user-submitted event to event_trail."""
    event_id = await append_event(
        conn=conn,
        org_id=org_id,
        project_id=project_id,
        event_type=body.event_type,
        severity=body.severity,
        payload=body.payload,
        service=body.service,
        resource=body.resource,
        environment=body.environment,
        trace_id=body.trace_id,
        initiated_by=body.initiated_by,
    )
    return {"id": str(event_id)}


@router.get("")
async def list_events(
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = Query(default=None),
    service: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=50, ge=1, le=500),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> list[dict[str, Any]]:
    """List events. viewer+ can read. project_id=None returns all projects in this org."""
    return await recent_events(
        conn=conn,
        org_id=org_id,
        project_id=project_id,
        service=service,
        hours=hours,
        limit=limit,
    )


@router.get("/{event_id}")
async def get_event(
    org_id: uuid.UUID,
    event_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> dict[str, Any]:
    """Fetch a single event by ID. viewer+ can read."""
    row = await conn.fetchrow(
        """
        SELECT id, ts, event_type, severity, payload,
               omodul_kind, autoheal_plugin, trace_id
          FROM event_trail
         WHERE id = $1 AND org_id = $2
        """,
        event_id,
        org_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found",
        )
    return dict(row)


@router.get("/{event_id}/causal-chain")
async def get_causal_chain(
    org_id: uuid.UUID,
    event_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> list[dict[str, Any]]:
    """Walk causal chain from event_id. viewer+ can read."""
    chain = await causal_chain(conn=conn, event_id=event_id)
    if not chain:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found",
        )
    return chain
