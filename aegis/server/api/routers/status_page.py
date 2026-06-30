"""Incident metrics (MTTA/MTTR) + a public status page.

- GET /orgs/{org_id}/incident-metrics — authenticated MTTA/MTTR + counts (viewer+).
- GET /status/{org_slug}             — PUBLIC, unauthenticated: open incidents only,
                                        sanitized to title/severity/started_at.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(tags=["incident-status"])


@router.get("/api/v1/orgs/{org_id}/incident-metrics")
async def incident_metrics(
    org_id: uuid.UUID,
    days: float = Query(default=30, gt=0, le=365),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> dict[str, Any]:
    """MTTA / MTTR (seconds) + counts over the last `days`. viewer+."""
    row = await conn.fetchrow(
        """
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE status = 'open') AS open,
            count(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved,
            avg(EXTRACT(EPOCH FROM (acknowledged_at - started_at)))
                FILTER (WHERE acknowledged_at IS NOT NULL) AS mtta_seconds,
            avg(EXTRACT(EPOCH FROM (resolved_at - started_at)))
                FILTER (WHERE resolved_at IS NOT NULL) AS mttr_seconds
          FROM incidents
         WHERE org_id = $1
           AND started_at >= now() - ($2::double precision * interval '1 day')
        """,
        org_id,
        days,
    )

    def _round(v: Any) -> float | None:
        return round(float(v), 1) if v is not None else None

    return {
        "days": days,
        "total": row["total"],
        "open": row["open"],
        "resolved": row["resolved"],
        "mtta_seconds": _round(row["mtta_seconds"]),
        "mttr_seconds": _round(row["mttr_seconds"]),
    }


@router.get("/api/v1/status/{org_slug}")
async def public_status(
    org_slug: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> dict[str, Any]:
    """PUBLIC status page — overall state + open incidents (sanitized). No auth."""
    org = await conn.fetchrow("SELECT id FROM orgs WHERE slug = $1", org_slug)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown status page")

    rows = await conn.fetch(
        "SELECT title, severity, started_at FROM incidents"
        " WHERE org_id = $1 AND status = 'open'"
        " ORDER BY started_at DESC LIMIT 50",
        org["id"],
    )
    incidents = [
        {"title": r["title"], "severity": r["severity"], "started_at": r["started_at"]}
        for r in rows
    ]
    if any(i["severity"] == "critical" for i in incidents):
        overall = "major_outage"
    elif incidents:
        overall = "degraded"
    else:
        overall = "operational"
    return {"status": overall, "open_incidents": incidents}
