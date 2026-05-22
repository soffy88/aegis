"""Alert ingestion endpoint — triggers Brain pipeline + AutoHeal."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn, require_org, require_project
from aegis.server.orchestration import run_brain_pipeline
from aegis.server.persistence import append_event

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


class AlertIngest(BaseModel):
    alert_name: str
    severity: str = "warning"
    service: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_alert(
    body: AlertIngest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    org_id: uuid.UUID = Depends(require_org),
    project_id: uuid.UUID = Depends(require_project),
) -> dict[str, Any]:
    """Ingest an alert. Writes alert_fired event + triggers Brain pipeline (stub)."""
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

    pipeline = await run_brain_pipeline(
        conn=conn,
        org_id=org_id,
        project_id=project_id,
        user_id=None,
        alert_payload={
            "alert_name": body.alert_name,
            "severity": body.severity,
            **body.payload,
        },
        trace_id=trace_id,
    )

    return {
        "trace_id": trace_id,
        "alert_event_id": str(alert_event_id),
        "brain_pipeline": pipeline,
    }
