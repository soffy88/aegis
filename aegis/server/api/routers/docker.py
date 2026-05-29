"""Docker container management API (走 oprim)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from oprim import (
    docker_container_inspect,
    docker_container_logs,
    docker_container_restart,
    docker_container_start,
    docker_container_stats,
    docker_container_stop,
)
from oprim._exceptions import OprimError

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission

router = APIRouter(prefix="/api/v1/orgs/{org_id}/docker", tags=["docker"])

_502 = status.HTTP_502_BAD_GATEWAY


@router.get("/containers/{container}")
async def inspect_container(
    org_id: UUID,
    container: str,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Inspect a container. viewer+ can read."""
    try:
        result = await asyncio.to_thread(docker_container_inspect, container_id=container)
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/start", status_code=status.HTTP_200_OK)
async def start_container(
    org_id: UUID,
    container: str,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Start a container. operator+ required."""
    try:
        result = await asyncio.to_thread(docker_container_start, container_id=container)
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/stop", status_code=status.HTTP_200_OK)
async def stop_container(
    org_id: UUID,
    container: str,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Stop a container. operator+ required."""
    try:
        result = await asyncio.to_thread(docker_container_stop, container_id=container)
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/restart", status_code=status.HTTP_200_OK)
async def restart_container(
    org_id: UUID,
    container: str,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Restart a container. operator+ required."""
    try:
        result = await asyncio.to_thread(docker_container_restart, container_id=container)
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.get("/containers/{container}/logs")
async def container_logs(
    org_id: UUID,
    container: str,
    tail: int = Query(default=100, ge=1, le=2000),
    since_seconds: int | None = Query(default=None, ge=1),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get container logs. viewer+ can read."""
    try:
        since = f"{since_seconds}s" if since_seconds else None
        result = await asyncio.to_thread(
            docker_container_logs, container_id=container, lines=tail, since=since
        )
        return {"container": container, "lines": [line.model_dump() for line in result]}
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.get("/containers/{container}/stats")
async def container_stats(
    org_id: UUID,
    container: str,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Single-shot container stats via oprim. viewer+ can read."""
    try:
        result = await asyncio.to_thread(docker_container_stats, container_id=container)
        s = result.model_dump()
        return {
            "container": container,
            "cpu_pct": s["cpu_percent"],
            "mem_mb": round(s["memory_usage_bytes"] / 1024 / 1024, 1),
            "mem_limit_mb": round(s["memory_limit_bytes"] / 1024 / 1024, 1),
            "net_rx_kb": round(s["network_rx_bytes"] / 1024, 1),
            "net_tx_kb": round(s["network_tx_bytes"] / 1024, 1),
        }
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc
