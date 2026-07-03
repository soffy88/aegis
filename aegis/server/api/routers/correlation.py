"""Alert / event correlation — group related events (by service+resource+type)
within time windows to cut noise, reporting a noise-reduction ratio."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/correlation", tags=["correlation"])


@router.get("")
async def correlate(
    org_id: uuid.UUID,
    minutes: int = Query(default=60, ge=1, le=1440),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Collapse raw events into correlated groups. A group = same
    service+resource+event_type in the window; count = how many raw events it
    absorbed (the noise reduction)."""
    groups = await conn.fetch(
        """
        SELECT coalesce(service,'-') AS service, coalesce(resource,'-') AS resource,
               event_type, max(severity) AS severity, count(*) AS events,
               min(ts) AS first_seen, max(ts) AS last_seen
          FROM event_trail
         WHERE org_id = $1 AND ts > now() - ($2 || ' minutes')::interval
         GROUP BY service, resource, event_type
         ORDER BY count(*) DESC
         LIMIT 200
        """,
        org_id,
        str(minutes),
    )
    total = sum(g["events"] for g in groups)
    reduction = round((1 - len(groups) / total) * 100, 1) if total else 0.0
    return {
        "raw_events": total,
        "groups": len(groups),
        "noise_reduction_pct": reduction,
        "correlated": [
            {
                "service": g["service"],
                "resource": g["resource"],
                "event_type": g["event_type"],
                "severity": g["severity"],
                "events": g["events"],
                "first_seen": g["first_seen"].isoformat(),
                "last_seen": g["last_seen"].isoformat(),
            }
            for g in groups
        ],
    }
