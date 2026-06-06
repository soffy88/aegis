"""Incidents API — list, get, generate postmortem."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

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
               postmortem_md, created_at
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
               postmortem_md, created_at
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
