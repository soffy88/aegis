"""Docker container management API (走 oprim)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from oprim import (
    docker_container_inspect,
    docker_container_logs,
    docker_container_restart,
    docker_container_start,
    docker_container_stats,
    docker_container_stop,
)
from oprim._exceptions import OprimError

router = APIRouter(prefix="/api/v1/docker", tags=["docker"])

_502 = status.HTTP_502_BAD_GATEWAY


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/containers/{container}")
async def inspect_container(container: str) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(docker_container_inspect, container_id=container)
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/start", status_code=status.HTTP_200_OK)
async def start_container(container: str) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(docker_container_start, container_id=container)
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/stop", status_code=status.HTTP_200_OK)
async def stop_container(container: str) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(docker_container_stop, container_id=container)
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/restart", status_code=status.HTTP_200_OK)
async def restart_container(container: str) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(docker_container_restart, container_id=container)
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.get("/containers/{container}/logs")
async def container_logs(
    container: str,
    tail: int = Query(default=100, ge=1, le=2000),
    since_seconds: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    try:
        since = f"{since_seconds}s" if since_seconds else None
        result = await asyncio.to_thread(
            docker_container_logs, container_id=container, lines=tail, since=since
        )
        return {"container": container, "lines": [line.model_dump() for line in result]}
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.get("/containers/{container}/stats")
async def container_stats(container: str) -> dict[str, Any]:
    """Single-shot container stats via oprim."""
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
