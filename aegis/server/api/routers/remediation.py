"""Remediation learning read API — what worked for a symptom (viewer+)."""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, Query

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/remediation-stats", tags=["remediation"])


@router.get("")
async def get_remediation_stats(
    org_id: uuid.UUID,
    symptom: str = Query(..., min_length=1, description="Symptom / alert name"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_EVENTS)),
) -> dict[str, Any]:
    """Per-remediation success rates learned for a symptom, best first. viewer+."""
    from aegis.server.services.remediation_learning import success_stats, symptom_key

    stats = await success_stats(conn, org_id=org_id, symptom=symptom)
    return {"symptom_key": symptom_key(symptom), "remediations": stats}
