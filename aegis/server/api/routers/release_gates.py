"""Release gates router."""

from __future__ import annotations

import uuid
from typing import Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.engines.release_gate_service import ReleaseGateService
from aegis.server.repositories.release_gate_repository import ReleaseGateRepository
from aegis.server.schemas.release_gate import (
    ReleaseGateCreate,
    ReleaseGateDecide,
    ReleaseGateResponse,
)

router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/projects/{project_id}/release-gates",
    tags=["release-gates"],
)


@router.post("", response_model=ReleaseGateResponse, status_code=status.HTTP_201_CREATED)
async def create_release_gate(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    data: ReleaseGateCreate,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> ReleaseGateResponse:
    """Create a release_gate.

    AutoHeal Engine calls this programmatically; manual creation allowed for M1 debugging.
    """
    repo = ReleaseGateRepository(conn)
    service = ReleaseGateService(repo)
    try:
        return await service.create_gate(
            org_id=org_id,
            project_id=project_id,
            requested_by=user.user_id,
            action_kind=data.action_kind,
            action_payload=data.action_payload,
            autoheal_event_id=data.autoheal_event_id,
            expires_in_hours=data.expires_in_hours,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "release_gate for this autoheal_event already exists",
        ) from exc


@router.get("", response_model=list[ReleaseGateResponse])
async def list_release_gates(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    state: Literal["pending", "approved", "rejected", "expired"] | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[ReleaseGateResponse]:
    """List release_gates with optional state filter. Applies lazy expiry before returning."""
    repo = ReleaseGateRepository(conn)
    return await repo.list_by_project(
        org_id=org_id,
        project_id=project_id,
        state=state,
        limit=limit,
    )


@router.get("/{gate_id}", response_model=ReleaseGateResponse)
async def get_release_gate(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    gate_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> ReleaseGateResponse:
    """Get a single release_gate. Applies lazy expiry."""
    repo = ReleaseGateRepository(conn)
    gate = await repo.get(gate_id=gate_id, org_id=org_id)
    if not gate or gate.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "release_gate not found")
    return gate


@router.post("/{gate_id}/decide", response_model=ReleaseGateResponse)
async def decide_release_gate(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    gate_id: uuid.UUID,
    data: ReleaseGateDecide,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> ReleaseGateResponse:
    """Approve or reject a release_gate.

    - 404: gate not found or wrong project
    - 409: gate already decided or expired
    """
    repo = ReleaseGateRepository(conn)
    service = ReleaseGateService(repo)

    existing = await repo.get(gate_id=gate_id, org_id=org_id)
    if not existing or existing.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "release_gate not found")

    if data.decision == "approved":
        result = await service.approve(
            gate_id=gate_id,
            org_id=org_id,
            decided_by=user.user_id,
            decision_reason=data.decision_reason,
        )
    else:
        result = await service.reject(
            gate_id=gate_id,
            org_id=org_id,
            decided_by=user.user_id,
            decision_reason=data.decision_reason,
        )

    if result is None:
        latest = await repo.get(gate_id=gate_id, org_id=org_id, lazy_expire=False)
        detail = (
            f"cannot decide release_gate in state '{latest.state}'"
            if latest and latest.state != "pending"
            else "release_gate decide failed (may be expired)"
        )
        raise HTTPException(status.HTTP_409_CONFLICT, detail)

    return result
