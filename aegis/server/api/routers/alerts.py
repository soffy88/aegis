"""Alert ingestion + management API."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.dispatch import OmodulDispatcher
from aegis.server.dispatch.budget_tracker import BudgetTracker
from aegis.server.dispatch.dedup_cache import DedupCache
from aegis.server.orchestration import run_brain_pipeline
from aegis.server.persistence import append_event
from aegis.server.runtime.config import AegisSettings

router = APIRouter(prefix="/api/v1/orgs/{org_id}/alerts", tags=["alerts"])


class AlertIngest(BaseModel):
    alert_name: str
    severity: str = "warning"
    service: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_alert(
    org_id: uuid.UUID,
    body: AlertIngest,
    project_id: uuid.UUID = Query(..., description="Project this alert belongs to"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_ALERT)),
) -> dict[str, Any]:
    """Ingest an alert. Writes alert_fired event + triggers Brain pipeline. member+ required."""
    trace_id = f"trc_{secrets.token_hex(8)}"

    alert_event_id = await append_event(
        conn=conn,
        org_id=org_id,
        project_id=project_id,
        event_type="alert_fired",
        severity=body.severity,
        service=body.service,
        payload={
            "alert_name": body.alert_name,
            **body.payload,
        },
        trace_id=trace_id,
        initiated_by="agent",
    )

    # Auto-cluster this alert into an incident (dedup by service + name).
    from aegis.server.services.incident_correlation import cluster_signal  # noqa: PLC0415

    incident_id, incident_is_new = await cluster_signal(
        conn,
        org_id=org_id,
        dedup_key=f"alert:{body.service or '-'}:{body.alert_name}",
        title=f"{body.alert_name}" + (f" on {body.service}" if body.service else ""),
        severity=body.severity,
        event_id=alert_event_id,
    )

    settings = AegisSettings()
    redis_client = aioredis.from_url(settings.redis_url)
    dispatcher = OmodulDispatcher(
        DedupCache(redis_client),
        BudgetTracker(redis_client),
        data_dir=str(settings.data_dir),
    )

    pipeline = await run_brain_pipeline(
        dispatcher=dispatcher,
        alert_payload={
            "alert_name": body.alert_name,
            "severity": body.severity,
            **body.payload,
        },
        context=body.payload,
        user_id=str(org_id),
    )

    await redis_client.aclose()

    return {
        "trace_id": trace_id,
        "alert_event_id": str(alert_event_id),
        "incident_id": str(incident_id),
        "incident_is_new": incident_is_new,
        "brain_pipeline": pipeline,
    }


@router.get("")
async def list_alerts(
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List alert_fired events for this org. viewer+ can read.
    project_id=None returns alerts across all projects in the org.
    """
    rows = await conn.fetch(
        """
        SELECT et.id, et.ts, et.project_id, et.severity, et.service,
               et.payload, et.trace_id
          FROM event_trail et
         WHERE et.org_id = $1
           AND et.event_type = 'alert_fired'
           AND ($2::uuid IS NULL OR et.project_id = $2)
         ORDER BY et.ts DESC
         LIMIT $3
        """,
        org_id,
        project_id,
        limit,
    )
    return [dict(r) for r in rows]


@router.post("/{event_id}/dismiss", status_code=status.HTTP_200_OK)
async def dismiss_alert(
    org_id: uuid.UUID,
    event_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.DISMISS_ALERT)),
) -> dict[str, Any]:
    """Dismiss an alert by appending an alert_acked event. viewer+ can dismiss (RFC v2.1 §4.2)."""
    # Verify the event belongs to this org and is an alert
    row = await conn.fetchrow(
        """
        SELECT id, project_id FROM event_trail
         WHERE id = $1 AND org_id = $2 AND event_type = 'alert_fired'
        """,
        event_id,
        org_id,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="alert not found in this org",
        )

    ack_id = await append_event(
        conn=conn,
        org_id=org_id,
        project_id=row["project_id"],
        event_type="alert_acked",
        severity="info",
        payload={"dismissed_by": str(user.user_id), "alert_event_id": str(event_id)},
        parent_id=event_id,
        initiated_by="user",
        user_id=user.user_id,
    )
    return {"message": "alert dismissed", "ack_event_id": str(ack_id)}
