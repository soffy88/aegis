"""Runbook API — list, execute, approve runbooks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from aegis.server.services.runbook import (
    approve_execution,
    execute_runbook,
    get_execution,
    get_runbook,
    list_runbooks,
)

router = APIRouter(prefix="/api/v1/runbooks", tags=["runbooks"])


class ExecuteRequest(BaseModel):
    dry_run: bool = True


@router.get("")
async def list_all_runbooks() -> list[dict[str, Any]]:
    return [rb.model_dump() for rb in list_runbooks()]


@router.get("/{name}")
async def get_runbook_detail(name: str) -> dict[str, Any]:
    rb = get_runbook(name)
    if not rb:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Runbook '{name}' not found"
        )
    return rb.model_dump()


@router.post("/{name}/execute")
async def execute(name: str, body: ExecuteRequest) -> dict[str, Any]:
    try:
        execution = await execute_runbook(name, dry_run=body.dry_run)
        return execution.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/executions/{exec_id}/approve")
async def approve(exec_id: str) -> dict[str, Any]:
    execution = approve_execution(exec_id)
    if not execution:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found or not awaiting approval",
        )
    # After approval, re-execute live
    result = await execute_runbook(execution.runbook_name, dry_run=False)
    return result.model_dump()


@router.get("/executions/{exec_id}")
async def get_execution_status(exec_id: str) -> dict[str, Any]:
    execution = get_execution(exec_id)
    if not execution:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return execution.model_dump()
