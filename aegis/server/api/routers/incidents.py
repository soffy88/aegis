"""Incidents API — list, get, generate postmortem."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.orchestration.postmortem import run_postmortem

router = APIRouter(prefix="/api/v1/orgs/{org_id}/incidents", tags=["incidents"])


@router.get("")
async def list_incidents(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> list[dict[str, Any]]:
    """List incidents for an org. viewer+ can read."""
    rows = await conn.fetch(
        """
        SELECT id, org_id, title, started_at, resolved_at, severity, status,
               postmortem_md, created_at, dedup_key, event_count, last_event_at
          FROM incidents
         WHERE org_id = $1
         ORDER BY started_at DESC
         LIMIT 200
        """,
        org_id,
    )
    return [dict(r) for r in rows]


@router.get("/{incident_id}")
async def get_incident(
    org_id: uuid.UUID,
    incident_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> dict[str, Any]:
    """Get incident detail including linked events. viewer+ can read."""
    row = await conn.fetchrow(
        """
        SELECT id, org_id, title, started_at, resolved_at, severity, status,
               postmortem_md, created_at, dedup_key, event_count, last_event_at
          FROM incidents
         WHERE id = $1 AND org_id = $2
        """,
        incident_id,
        org_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")

    events = await conn.fetch(
        """
        SELECT id, ts, event_type, severity, service, payload, trace_id
          FROM event_trail
         WHERE root_cause_id = $1 OR id = $1
         ORDER BY ts ASC
         LIMIT 500
        """,
        incident_id,
    )
    result = dict(row)
    result["events"] = [dict(e) for e in events]
    return result


@router.get("/{incident_id}/events")
async def list_incident_events(
    org_id: uuid.UUID,
    incident_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> list[dict[str, Any]]:
    """Signals clustered into this incident (via incident_events). viewer+."""
    owns = await conn.fetchval(
        "SELECT 1 FROM incidents WHERE id = $1 AND org_id = $2", incident_id, org_id
    )
    if not owns:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    rows = await conn.fetch(
        """
        SELECT et.id, et.ts, et.event_type, et.severity, et.service, et.payload, et.trace_id
          FROM incident_events ie
          JOIN event_trail et ON et.id = ie.event_id
         WHERE ie.incident_id = $1
         ORDER BY et.ts ASC
         LIMIT 500
        """,
        incident_id,
    )
    return [dict(r) for r in rows]


class _IncidentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    severity: str = "warning"
    dedup_key: str | None = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_incident(
    org_id: uuid.UUID,
    body: _IncidentCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Manually open an incident. operator+ required."""
    row = await conn.fetchrow(
        "INSERT INTO incidents (org_id, title, severity, status, dedup_key, event_count)"
        " VALUES ($1, $2, $3, 'open', $4, 0)"
        " RETURNING id, org_id, title, started_at, resolved_at, severity, status,"
        " postmortem_md, created_at, dedup_key, event_count, last_event_at",
        org_id,
        body.title,
        body.severity,
        body.dedup_key,
    )
    return dict(row)


@router.post("/{incident_id}/resolve")
async def resolve_incident(
    org_id: uuid.UUID,
    incident_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Resolve an incident (frees its dedup_key so future signals open a fresh one)."""
    row = await conn.fetchrow(
        "UPDATE incidents SET status = 'resolved', resolved_at = now()"
        " WHERE id = $1 AND org_id = $2 AND status = 'open'"
        " RETURNING id, status, resolved_at",
        incident_id,
        org_id,
    )
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "open incident not found"
        )
    return dict(row)


@router.post("/{incident_id}/postmortem")
async def generate_postmortem(
    org_id: uuid.UUID,
    incident_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Generate or regenerate a postmortem for an incident. operator+ required."""
    incident = await conn.fetchrow(
        """
        SELECT id, org_id, title, started_at, resolved_at, severity, status, postmortem_md
          FROM incidents
         WHERE id = $1 AND org_id = $2
        """,
        incident_id,
        org_id,
    )
    if incident is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")

    events = await conn.fetch(
        """
        SELECT id, ts, event_type, severity, service, payload, trace_id
          FROM event_trail
         WHERE root_cause_id = $1 OR id = $1
         ORDER BY ts ASC
         LIMIT 500
        """,
        incident_id,
    )

    postmortem_md = await run_postmortem(
        incident=dict(incident),
        events=[dict(e) for e in events],
    )

    await conn.execute(
        "UPDATE incidents SET postmortem_md = $2 WHERE id = $1",
        incident_id,
        postmortem_md,
    )

    return {
        "incident_id": str(incident_id),
        "postmortem_md": postmortem_md,
    }
