"""Runbook API — list, execute, approve runbooks."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.repositories.project_repo import ProjectRepository
from aegis.server.services.runbook import (
    approve_execution,
    execute_runbook,
    get_execution,
    get_runbook,
    list_runbooks,
)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/runbooks", tags=["runbooks"])


class ExecuteRequest(BaseModel):
    dry_run: bool = True


@router.get("")
async def list_all_runbooks(
    org_id: UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List available runbooks. viewer+ can read."""
    return [rb.model_dump() for rb in list_runbooks()]


# Register /executions/{exec_id} BEFORE /{name} to avoid path-param shadowing
@router.post("/executions/{exec_id}/approve")
async def approve(
    org_id: UUID,
    exec_id: str,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Approve a pending execution. operator+ required."""
    execution = approve_execution(exec_id)
    if not execution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found or not awaiting approval",
        )
    result = await execute_runbook(execution.runbook_name, dry_run=False)
    return result.model_dump()


@router.get("/executions/{exec_id}")
async def get_execution_status(
    org_id: UUID,
    exec_id: str,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get execution status. viewer+ can read."""
    execution = get_execution(exec_id)
    if not execution:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return execution.model_dump()


@router.get("/{name}")
async def get_runbook_detail(
    org_id: UUID,
    name: str,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get runbook details. viewer+ can read."""
    rb = get_runbook(name)
    if not rb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Runbook '{name}' not found"
        )
    return rb.model_dump()


@router.post("/{name}/execute")
async def execute(
    org_id: UUID,
    name: str,
    body: ExecuteRequest,
    project_id: UUID = Query(..., description="Project context for this runbook execution"),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> dict[str, Any]:
    """Execute a runbook. operator+ required."""
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found in this org"
        )

    try:
        execution = await execute_runbook(name, dry_run=body.dry_run)
        return execution.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
