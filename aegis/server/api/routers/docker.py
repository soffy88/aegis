"""Docker container management API (走 oprim)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
import asyncpg
from obase.auth import jwt_verify_hs256
from obase.docker import (
    docker_container_exec,
    docker_container_inspect,
    docker_container_logs,
    docker_container_restart,
    docker_container_start,
    docker_container_stats,
    docker_container_stop,
    docker_image_delete,
    docker_image_list,
    docker_image_pull,
    docker_network_create,
    docker_network_delete,
    docker_network_list,
    docker_ps,
    docker_system_prune,
    docker_volume_create,
    docker_volume_delete,
    docker_volume_list,
)
from oprim._exceptions import OprimError
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_min_role, require_permission
from aegis.server.models import ROLE_HIERARCHY, Role
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/docker", tags=["docker"])

_502 = status.HTTP_502_BAD_GATEWAY


async def _resolve_docker_host(
    conn: asyncpg.Connection, org_id: UUID, node_id: UUID | None
) -> str | None:
    """Resolve the target Docker daemon for a request.

    node_id=None → return None so the oprim call OMITS docker_host and uses its own
    default exactly as it did before multi-host routing existed (avoids overriding a
    deployment whose working daemon isn't settings.docker_host). A node_id routes to
    that node's docker_host_url so multi-host control works.
    """
    if node_id is None:
        return None
    row = await conn.fetchrow(
        "SELECT docker_host_url FROM aegis_nodes WHERE org_id = $1 AND node_id = $2",
        org_id,
        node_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    return row["docker_host_url"] or get_settings().docker_host


def _hostkw(docker_host: str | None) -> dict[str, str]:
    """Pass docker_host to oprim only when a specific host was resolved; otherwise
    omit it so oprim uses its own default (pre-multi-host behavior)."""
    return {"docker_host": docker_host} if docker_host else {}


class NetworkCreateRequest(BaseModel):
    name: str
    driver: str = "bridge"
    internal: bool = False
    labels: dict[str, str] | None = None
    options: dict[str, str] | None = None


class VolumeCreateRequest(BaseModel):
    name: str
    driver: str = "local"
    labels: dict[str, str] | None = None
    driver_opts: dict[str, str] | None = None


class ContainerExecRequest(BaseModel):
    command: list[str]
    workdir: str | None = None
    env: dict[str, str] | None = None
    user: str | None = None
    timeout_sec: int = 30


class ImagePullRequest(BaseModel):
    image: str
    tag: str = "latest"


@router.get("/containers")
async def list_containers(
    org_id: UUID,
    all: bool = Query(default=False, description="Include stopped containers"),
    node_id: UUID | None = Query(default=None, description="Target node; omit for platform host"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List containers via oprim docker_ps. viewer+ can read."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        items = await asyncio.to_thread(docker_ps, all=all, **_hostkw(docker_host))
        return [c.model_dump() if hasattr(c, "model_dump") else c for c in items]
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.get("/containers/{container}")
async def inspect_container(
    org_id: UUID,
    container: str,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Inspect a container. viewer+ can read."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        result = await asyncio.to_thread(
            docker_container_inspect, container_id=container, **_hostkw(docker_host)
        )
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/start", status_code=status.HTTP_200_OK)
async def start_container(
    org_id: UUID,
    container: str,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Start a container. operator+ required."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        result = await asyncio.to_thread(
            docker_container_start, container_id=container, **_hostkw(docker_host)
        )
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/stop", status_code=status.HTTP_200_OK)
async def stop_container(
    org_id: UUID,
    container: str,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Stop a container. operator+ required."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        result = await asyncio.to_thread(
            docker_container_stop, container_id=container, **_hostkw(docker_host)
        )
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/restart", status_code=status.HTTP_200_OK)
async def restart_container(
    org_id: UUID,
    container: str,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Restart a container. operator+ required."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        result = await asyncio.to_thread(
            docker_container_restart, container_id=container, **_hostkw(docker_host)
        )
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.get("/containers/{container}/logs")
async def container_logs(
    org_id: UUID,
    container: str,
    tail: int = Query(default=100, ge=1, le=2000),
    since_seconds: int | None = Query(default=None, ge=1),
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get container logs. viewer+ can read."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        since = f"{since_seconds}s" if since_seconds else None
        result = await asyncio.to_thread(
            docker_container_logs,
            container_id=container,
            lines=tail,
            since=since,
            **_hostkw(docker_host),
        )
        return {"container": container, "lines": [line.model_dump() for line in result]}
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.get("/logs/search")
async def search_logs(
    org_id: UUID,
    q: str = Query(default="", description="case-insensitive substring filter"),
    containers: str = Query(default="", description="comma-separated names; empty = all running"),
    tail: int = Query(default=200, ge=1, le=1000),
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Aggregate + search recent logs across multiple containers (self-hosted log
    aggregation without an external Loki/ES stack)."""
    from obase.docker import docker_container_list  # noqa: PLC0415

    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    names = [c.strip() for c in containers.split(",") if c.strip()]
    if not names:
        try:
            lst = await asyncio.to_thread(docker_container_list, **_hostkw(docker_host))
            names = [getattr(c, "name", None) for c in lst if getattr(c, "name", None)][:25]
        except OprimError as exc:
            raise HTTPException(status_code=_502, detail=str(exc)) from exc
    ql = q.lower()
    rows: list[dict[str, Any]] = []
    for name in names[:25]:
        try:
            logs = await asyncio.to_thread(
                docker_container_logs, container_id=name, lines=tail, **_hostkw(docker_host)
            )
        except Exception:  # noqa: BLE001 — skip containers we can't read
            continue
        for ll in logs:
            d = ll.model_dump()
            msg = str(d.get("message") or d.get("line") or "")
            if not ql or ql in msg.lower():
                rows.append({"container": name, "timestamp": d.get("timestamp"), "message": msg})
    rows.sort(key=lambda r: r.get("timestamp") or "")
    return {"total": len(rows), "lines": rows[-1000:]}


@router.get("/containers/{container}/stats")
async def container_stats(
    org_id: UUID,
    container: str,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Single-shot container stats via oprim. viewer+ can read."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        result = await asyncio.to_thread(
            docker_container_stats, container_id=container, **_hostkw(docker_host)
        )
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


# ── Memory management ─────────────────────────────────────────────────────────
# oprim has no container-update primitive and 3O libs are off-limits, so memory
# limits are set at the aegis layer via the docker CLI (staged into the image),
# the same pattern apps.py uses to shell to `docker compose`.

_UNITS = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
}


def _parse_size(s: str) -> float:
    """'36.32MiB' / '1.5GiB' / '0B' → bytes. Returns 0.0 on unparseable input."""
    s = s.strip()
    for unit in sorted(_UNITS, key=len, reverse=True):
        if s.upper().endswith(unit):
            try:
                return float(s[: -len(unit)].strip()) * _UNITS[unit]
            except ValueError:
                return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _docker_cli(docker_host: str | None, args: list[str], timeout: int = 30) -> str:
    """Run `docker [-H host] <args>` and return stdout. Raises 502 on failure."""
    import subprocess  # noqa: PLC0415

    dh = docker_host or get_settings().docker_host
    cmd = ["docker", "-H", dh, *args]
    try:
        r = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # noqa: S607
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(_502, f"docker cli failed: {exc}") from exc
    if r.returncode != 0:
        raise HTTPException(_502, f"docker: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


class ContainerLimitsRequest(BaseModel):
    # Megabytes. null → remove the limit (unlimited). memory_swap_mb defaults to
    # memory_mb (i.e. no extra swap) when a memory limit is set.
    memory_mb: int | None = None
    memory_swap_mb: int | None = None


@router.post("/containers/{container}/limits", status_code=status.HTTP_200_OK)
async def set_container_limits(
    org_id: UUID,
    container: str,
    body: ContainerLimitsRequest,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Set a container's memory limit via `docker update`. operator+.

    memory_swap defaults to memory_mb (no extra swap) unless memory_swap_mb is
    given explicitly (>= memory_mb). The limit applies to the live container; it
    is reset if the container is recreated (e.g. `compose up`), so for permanent
    caps also set the limit in the container's compose/run definition.

    Note: Docker cannot *remove* a memory limit from a live container
    (`docker update --memory 0` is a no-op); memory_mb=null is rejected. To go
    back to unlimited, recreate the container without a limit.
    """
    if body.memory_mb is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Docker cannot unset a memory limit on a live container; recreate it "
            "(e.g. compose up) without a limit instead.",
        )
    if body.memory_mb < 6:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "memory_mb must be >= 6")
    swap = body.memory_swap_mb if body.memory_swap_mb is not None else body.memory_mb
    if swap < body.memory_mb:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "memory_swap_mb must be >= memory_mb")
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    args = ["update", "--memory", f"{body.memory_mb}m", "--memory-swap", f"{swap}m", container]
    await asyncio.to_thread(_docker_cli, docker_host, args)
    return {"container": container, "memory_mb": body.memory_mb, "ok": True}


async def _host_memory(conn: asyncpg.Connection) -> dict[str, Any]:
    """Whole-host mem/swap summary from the latest node_exporter samples."""

    async def latest(metric: str) -> float | None:
        return await conn.fetchval(
            "SELECT value FROM agent_metrics WHERE metric_name = $1 ORDER BY ts DESC LIMIT 1",
            metric,
        )

    mt = await latest("node_memory_MemTotal_bytes")
    ma = await latest("node_memory_MemAvailable_bytes")
    st = await latest("node_memory_SwapTotal_bytes")
    sf = await latest("node_memory_SwapFree_bytes")
    mb = 1024 * 1024
    out: dict[str, Any] = {}
    if mt and ma is not None:
        out.update(
            mem_total_mb=round(mt / mb),
            mem_available_mb=round(ma / mb),
            mem_used_mb=round((mt - ma) / mb),
            mem_used_pct=round((1 - ma / mt) * 100, 1),
        )
    if st and sf is not None and st > 0:
        out.update(
            swap_total_mb=round(st / mb),
            swap_used_mb=round((st - sf) / mb),
            swap_used_pct=round((1 - sf / st) * 100, 1),
        )
    return out


@router.get("/memory/overview")
async def memory_overview(
    org_id: UUID,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Per-container memory usage vs limit + whole-host summary. viewer+.

    A container with no memory limit reports its limit as the host's total RAM;
    we flag those (has_limit=false) so they can be capped from the UI.
    """
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    host = await _host_memory(conn)
    host_total_bytes = host.get("mem_total_mb", 0) * 1024 * 1024

    out = await asyncio.to_thread(
        _docker_cli, docker_host, ["stats", "--no-stream", "--format", "{{json .}}"], 40
    )
    containers: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        used_s, _, limit_s = d.get("MemUsage", "").partition(" / ")
        used = _parse_size(used_s)
        limit = _parse_size(limit_s)
        # docker shows host-total as the limit for uncapped containers
        has_limit = bool(limit) and (not host_total_bytes or limit < host_total_bytes * 0.98)
        containers.append(
            {
                "name": d.get("Name"),
                "mem_mb": round(used / 1024 / 1024, 1),
                "limit_mb": round(limit / 1024 / 1024, 1) if has_limit else None,
                "pct_of_limit": round(used / limit * 100, 1) if has_limit and limit else None,
                "has_limit": has_limit,
            }
        )
    containers.sort(key=lambda c: c["mem_mb"], reverse=True)
    return {"host": host, "containers": containers}


@router.post("/networks", status_code=status.HTTP_201_CREATED)
async def create_network(
    org_id: UUID,
    req: NetworkCreateRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Create a docker network."""
    try:
        result = await asyncio.to_thread(
            docker_network_create,
            name=req.name,
            driver=req.driver,
            internal=req.internal,
            labels=req.labels,
            options=req.options,
        )
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.delete("/networks/{network_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_network(
    org_id: UUID,
    network_id: str,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> None:
    """Delete a docker network."""
    try:
        await asyncio.to_thread(docker_network_delete, network_id=network_id)
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/volumes", status_code=status.HTTP_201_CREATED)
async def create_volume(
    org_id: UUID,
    req: VolumeCreateRequest,
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Create a docker volume."""
    try:
        result = await asyncio.to_thread(
            docker_volume_create,
            name=req.name,
            driver=req.driver,
            labels=req.labels,
            driver_opts=req.driver_opts,
        )
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


# ── images (audit #11) ─────────────────────────────────────────────────────────


@router.get("/images")
async def list_images(
    org_id: UUID,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List images on the target daemon. viewer+ can read."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        return await asyncio.to_thread(docker_image_list, **_hostkw(docker_host))
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/images/pull", status_code=status.HTTP_200_OK)
async def pull_image(
    org_id: UUID,
    req: ImagePullRequest,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Pull an image onto the target daemon. operator+ required."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        result = await asyncio.to_thread(
            docker_image_pull, image=req.image, tag=req.tag, **_hostkw(docker_host)
        )
        return result.model_dump() if hasattr(result, "model_dump") else result
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.delete("/images/{image:path}", status_code=status.HTTP_200_OK)
async def delete_image(
    org_id: UUID,
    image: str,
    force: bool = Query(default=False),
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Delete an image from the target daemon. operator+ required."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        return await asyncio.to_thread(
            docker_image_delete, image=image, force=force, **_hostkw(docker_host)
        )
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/system/prune", status_code=status.HTTP_200_OK)
async def system_prune(
    org_id: UUID,
    volumes: bool = Query(default=False, description="Also prune unused volumes"),
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Reclaim space (dangling images, stopped containers, optionally volumes)."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        result = await asyncio.to_thread(
            docker_system_prune, volumes=volumes, **_hostkw(docker_host)
        )
        return result.model_dump() if hasattr(result, "model_dump") else result
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


# ── network / volume listing + deletion (audit #12) ──────────────────────────────


@router.get("/networks")
async def list_networks(
    org_id: UUID,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List docker networks on the target daemon. viewer+ can read."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        return await asyncio.to_thread(docker_network_list, **_hostkw(docker_host))
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.get("/volumes")
async def list_volumes(
    org_id: UUID,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List docker volumes on the target daemon. viewer+ can read."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        return await asyncio.to_thread(docker_volume_list, **_hostkw(docker_host))
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.delete("/volumes/{name}", status_code=status.HTTP_200_OK)
async def delete_volume(
    org_id: UUID,
    name: str,
    force: bool = Query(default=False),
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Delete a docker volume from the target daemon. operator+ required."""
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        return await asyncio.to_thread(
            docker_volume_delete, name=name, force=force, **_hostkw(docker_host)
        )
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/containers/{container}/exec", status_code=status.HTTP_200_OK)
async def exec_container(
    org_id: UUID,
    container: str,
    req: ContainerExecRequest,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_min_role(Role.ADMIN)),
) -> dict[str, Any]:
    """Execute a command in a container.

    Arbitrary in-container command execution is a host-operator capability (it can
    read another workload's secrets / spawn processes), so it is gated at admin+
    rather than operator — not every on-call operator should hold shell access.
    """
    docker_host = await _resolve_docker_host(conn, org_id, node_id)
    try:
        result = await asyncio.to_thread(
            docker_container_exec,
            container_id=container,
            command=req.command,
            workdir=req.workdir,
            env=req.env,
            user=req.user,
            timeout_sec=req.timeout_sec,
            **_hostkw(docker_host),
        )
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


@router.post("/host-shell", status_code=status.HTTP_200_OK)
async def ensure_host_shell(
    org_id: UUID,
    user: UserContext = Depends(require_min_role(Role.OWNER)),
) -> dict[str, Any]:
    """Ensure a privileged host-access helper container is running, then return it.

    The container shares the host PID namespace and bind-mounts the host root at
    /host, so the standard container terminal into it reaches the host (run
    `chroot /host bash`) — i.e. full host root. This is a break-glass capability
    gated on org owner only; combined with the admin+ gate on the terminal, a
    member/operator can no longer reach the host.
    """
    import subprocess  # noqa: PLC0415

    name = "aegis-host-shell"
    dh = get_settings().docker_host

    def _ensure() -> None:
        # Already running?
        ps = subprocess.run(  # noqa: S603
            ["docker", "-H", dh, "ps", "-q", "-f", f"name=^{name}$"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if ps.stdout.strip():
            return
        subprocess.run(["docker", "-H", dh, "rm", "-f", name], capture_output=True, check=False)  # noqa: S603, S607
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "-H",
                dh,
                "run",
                "-d",
                "--name",
                name,
                "--privileged",
                "--pid=host",
                "--network=host",
                "-v",
                "/:/host",
                "--restart",
                "unless-stopped",
                "alpine:latest",
                "sleep",
                "infinity",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

    try:
        await asyncio.to_thread(_ensure)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=_502, detail=f"host shell start failed: {exc}") from exc
    return {"container": name, "hint": "chroot /host bash"}


@router.websocket("/containers/{container_name}/terminal")
async def container_terminal(
    websocket: WebSocket,
    org_id: UUID,
    container_name: str,
    token: str,
) -> None:
    """Interactive container terminal (WebSocket + docker exec)."""
    # 1. Validate token AND authorize. A WS cannot carry an Authorization header,
    #    so we verify the token query-param manually — but must still enforce the
    #    same authz as the REST exec endpoint, not just signature validity:
    #    holder must present a valid *access* token, be a member of org_id, and
    #    be admin+ (mirrors exec_container's require_min_role(ADMIN) — an interactive
    #    shell is a host-operator capability, not an on-call operator one).
    #    NOTE: container_name is not yet scoped to org_id (containers are global on
    #    the platform daemon in this design). Cross-tenant container ownership
    #    enforcement is tracked separately; the admin+ gate limits the blast radius
    #    to trusted org admins in the meantime.
    try:
        payload = jwt_verify_hs256(
            token=token,
            secret=get_settings().jwt_secret,
            check_exp=True,
        )
    except Exception:
        await websocket.close(code=1008)  # Policy Violation
        return

    if payload.get("type") != "access":
        await websocket.close(code=1008)
        return

    membership = next(
        (o for o in payload.get("orgs", []) if o.get("org_id") == str(org_id)),
        None,
    )
    if membership is None:
        await websocket.close(code=1008)  # not a member of this org
        return
    try:
        role = Role(membership["role"])
    except (KeyError, ValueError):
        await websocket.close(code=1008)
        return
    if ROLE_HIERARCHY[role] < ROLE_HIERARCHY[Role.ADMIN]:
        await websocket.close(code=1008)  # interactive shell requires admin+
        return

    await websocket.accept()

    # 2. Establish docker exec interactive session
    import docker
    import docker.errors

    settings = get_settings()
    client = None
    try:
        try:
            client = docker.DockerClient(base_url=settings.docker_host)
            container = client.containers.get(container_name)
        except docker.errors.NotFound:
            await websocket.send_text(
                json.dumps({"type": "error", "data": f"Container '{container_name}' not found"})
            )
            await websocket.close()
            return
        except Exception as exc:
            await websocket.send_text(json.dumps({"type": "error", "data": str(exc)}))
            await websocket.close()
            return

        # 3. Create exec instance (interactive PTY)
        exec_id = client.api.exec_create(
            container_name,
            cmd="/bin/sh",
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
        )
        sock = client.api.exec_start(exec_id["Id"], socket=True, tty=True)
        sock._sock.setblocking(False)

        loop = asyncio.get_event_loop()

        async def read_docker() -> None:
            """Docker → WebSocket."""
            try:
                while True:
                    data = await loop.run_in_executor(None, _read_socket, sock._sock)
                    if data is None:
                        await asyncio.sleep(0.01)
                        continue
                    if not data:  # EOF
                        break
                    await websocket.send_text(
                        json.dumps(
                            {"type": "output", "data": data.decode("utf-8", errors="replace")}
                        )
                    )
            except Exception:
                log.exception("terminal_read_docker_error")

        async def read_ws() -> None:
            """WebSocket → Docker."""
            try:
                while True:
                    msg = await websocket.receive_text()
                    payload = json.loads(msg)
                    if payload.get("type") == "input":
                        await loop.run_in_executor(None, sock._sock.send, payload["data"].encode())
                    elif payload.get("type") == "resize":
                        client.api.exec_resize(
                            exec_id["Id"],
                            height=payload.get("rows", 24),
                            width=payload.get("cols", 80),
                        )
            except WebSocketDisconnect:
                pass
            except Exception:
                log.exception("terminal_read_ws_error")

        try:
            await asyncio.gather(read_docker(), read_ws())
        finally:
            sock.close()
            try:
                await websocket.close()
            except Exception:
                pass
    finally:
        if client is not None:
            client.close()


def _read_socket(sock: Any, size: int = 4096) -> bytes | None:
    """Non-blocking socket read. Returns None if no data."""

    try:
        return sock.recv(size)
    except (OSError, BlockingIOError):
        return None
