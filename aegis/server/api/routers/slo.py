"""SLO / error-budget tracking.

An SLO targets a success objective (e.g. 99.5%) for a service over a window. The
SLI is computed from ingested spans (success = non-error spans / total). Reports
current SLI, error-budget remaining and burn rate.

Note: SLI is computed over the trace data actually retained (~48h), so very long
windows are bounded by retention.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/slos", tags=["slo"])


class SLORequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    service: str = Field(..., min_length=1)
    objective: float = Field(..., gt=0, lt=100)  # target success %, e.g. 99.5
    window_days: int = Field(default=30, ge=1, le=90)


async def _compute(conn: asyncpg.Connection, row: asyncpg.Record) -> dict[str, Any]:
    stat = await conn.fetchrow(
        """SELECT count(*) AS total,
                  sum(CASE WHEN status_code = 2 THEN 1 ELSE 0 END) AS errors
             FROM aegis_spans
            WHERE service = $1
              AND org_id = $3
              AND ingested_at > now() - ($2 || ' days')::interval""",
        row["service"],
        str(row["window_days"]),
        row["org_id"],
    )
    total = stat["total"] or 0
    errors = stat["errors"] or 0
    allowed = (100 - row["objective"]) / 100  # allowed error fraction
    actual = (errors / total) if total else 0.0
    sli = (1 - actual) * 100 if total else None
    burn = (actual / allowed) if allowed > 0 else 0.0
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "service": row["service"],
        "objective": row["objective"],
        "window_days": row["window_days"],
        "sample_count": total,
        "current_sli": round(sli, 3) if sli is not None else None,
        "budget_remaining_pct": round(max(0.0, (1 - burn)) * 100, 1) if total else None,
        "burn_rate": round(burn, 2) if total else None,
        "meeting": (sli >= row["objective"]) if sli is not None else None,
    }


@router.get("")
async def list_slos(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch("SELECT * FROM aegis_slos WHERE org_id = $1 ORDER BY name", org_id)
    return [await _compute(conn, r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_slo(
    org_id: uuid.UUID,
    req: SLORequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict[str, str]:
    try:
        sid = await conn.fetchval(
            """INSERT INTO aegis_slos (org_id, name, service, objective, window_days)
               VALUES ($1,$2,$3,$4,$5) RETURNING id""",
            org_id,
            req.name,
            req.service,
            req.objective,
            req.window_days,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "SLO name already exists") from exc
    return {"id": str(sid), "status": "created"}


@router.delete("/{slo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_slo(
    org_id: uuid.UUID,
    slo_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> None:
    await conn.execute("DELETE FROM aegis_slos WHERE id = $1 AND org_id = $2", slo_id, org_id)
