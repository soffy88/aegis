"""event_trail HTTP API."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn, require_org, require_project
from aegis.server.persistence import (
    append_event,
    causal_chain,
    recent_events,
)

router = APIRouter(prefix="/api/v1/events", tags=["events"])


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
    body: EventCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    org_id: uuid.UUID = Depends(require_org),
    project_id: uuid.UUID = Depends(require_project),
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
    conn: asyncpg.Connection = Depends(get_db_conn),
    org_id: uuid.UUID = Depends(require_org),
    project_id: uuid.UUID = Depends(require_project),
    service: str | None = Query(default=None),
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    return await recent_events(
        conn=conn,
        org_id=org_id,
        project_id=project_id,
        service=service,
        hours=hours,
        limit=limit,
    )


@router.get("/{event_id}/causal-chain")
async def get_causal_chain(
    event_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> list[dict[str, Any]]:
    chain = await causal_chain(conn=conn, event_id=event_id)
    if not chain:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Event {event_id} not found",
        )
    return chain
