"""Alert fired history — read-only router."""

from __future__ import annotations

import uuid
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, Query

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.repositories.alert_fired_repository import AlertFiredRepository
from aegis.server.schemas.alerting import AlertFiredResponse

router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/projects/{project_id}/alerts-fired",
    tags=["alert-fired"],
)


@router.get("", response_model=list[AlertFiredResponse])
async def list_alerts_fired(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    severity: Literal["warn", "critical"] | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[AlertFiredResponse]:
    repo = AlertFiredRepository(conn)
    return await repo.list_by_project(
        org_id=org_id,
        project_id=project_id,
        limit=limit,
        severity=severity,
    )
