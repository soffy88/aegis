"""Docker container management API."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

router = APIRouter(prefix="/api/v1/docker", tags=["docker"])

_502 = status.HTTP_502_BAD_GATEWAY


# ---------------------------------------------------------------------------
# Async helpers — lazy-import oprim so tests can mock at this level
# ---------------------------------------------------------------------------


async def _inspect(container: str) -> dict[str, Any]:
    from oprim._exceptions import OprimError  # noqa: PLC0415
    from oprim.docker_container_inspect import docker_container_inspect  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(docker_container_inspect, container=container)
        return result.model_dump()  # type: ignore[no-any-return]
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


async def _start(container: str) -> dict[str, Any]:
    from oprim._exceptions import OprimError  # noqa: PLC0415
    from oprim.docker_container_start import docker_container_start  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(docker_container_start, container=container)
        return result.model_dump()  # type: ignore[no-any-return]
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


async def _stop(container: str) -> dict[str, Any]:
    from oprim._exceptions import OprimError  # noqa: PLC0415
    from oprim.docker_container_stop import docker_container_stop  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(docker_container_stop, container=container)
        return result.model_dump()  # type: ignore[no-any-return]
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


async def _restart(container: str) -> dict[str, Any]:
    from oprim._exceptions import OprimError  # noqa: PLC0415
    from oprim.docker_container_restart import docker_container_restart  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(docker_container_restart, container=container)
        return result.model_dump()  # type: ignore[no-any-return]
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


async def _logs(
    container: str,
    tail: int,
    since_seconds: int | None,
) -> dict[str, Any]:
    from oprim._exceptions import OprimError  # noqa: PLC0415
    from oprim.docker_logs import docker_logs  # noqa: PLC0415

    try:
        result = await asyncio.to_thread(
            docker_logs,
            container=container,
            tail=tail,
            since_seconds=since_seconds,
        )
        return result.model_dump()  # type: ignore[no-any-return]
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/containers/{container}")
async def inspect_container(container: str) -> dict[str, Any]:
    return await _inspect(container)


@router.post("/containers/{container}/start", status_code=status.HTTP_200_OK)
async def start_container(container: str) -> dict[str, Any]:
    return await _start(container)


@router.post("/containers/{container}/stop", status_code=status.HTTP_200_OK)
async def stop_container(container: str) -> dict[str, Any]:
    return await _stop(container)


@router.post("/containers/{container}/restart", status_code=status.HTTP_200_OK)
async def restart_container(container: str) -> dict[str, Any]:
    return await _restart(container)


@router.get("/containers/{container}/logs")
async def container_logs(
    container: str,
    tail: int = Query(default=100, ge=1, le=2000),
    since_seconds: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    return await _logs(container, tail, since_seconds)
