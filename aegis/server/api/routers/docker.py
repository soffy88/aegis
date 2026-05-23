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


@router.get("/containers/{container}/stats")
async def container_stats(container: str) -> dict[str, Any]:
    """Single-shot container stats: CPU%, Mem MB, Net I/O kB/s."""

    def _get_stats(name: str) -> dict[str, Any]:
        import docker  # noqa: PLC0415

        client = docker.from_env()
        c = client.containers.get(name)
        raw = c.stats(stream=False)

        # CPU %
        cpu_delta = (
            raw["cpu_stats"]["cpu_usage"]["total_usage"]
            - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        sys_delta = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"]["system_cpu_usage"]
        n_cpus = raw["cpu_stats"].get("online_cpus") or len(
            raw["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])
        )
        cpu_pct = round((cpu_delta / sys_delta) * n_cpus * 100, 2) if sys_delta > 0 else 0.0

        # Memory
        mem_usage = raw["memory_stats"].get("usage", 0)
        mem_limit = raw["memory_stats"].get("limit", 1)
        mem_mb = round(mem_usage / 1024 / 1024, 1)

        # Network I/O
        networks = raw.get("networks", {})
        rx_bytes = sum(v.get("rx_bytes", 0) for v in networks.values())
        tx_bytes = sum(v.get("tx_bytes", 0) for v in networks.values())

        return {
            "container": name,
            "cpu_pct": cpu_pct,
            "mem_mb": mem_mb,
            "mem_limit_mb": round(mem_limit / 1024 / 1024, 1),
            "net_rx_kb": round(rx_bytes / 1024, 1),
            "net_tx_kb": round(tx_bytes / 1024, 1),
        }

    try:
        return await asyncio.to_thread(_get_stats, container)
    except Exception as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc
