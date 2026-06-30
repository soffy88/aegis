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
from oprim import (
    docker_container_exec,
    docker_container_inspect,
    docker_container_logs,
    docker_container_restart,
    docker_container_start,
    docker_container_stats,
    docker_container_stop,
    docker_network_create,
    docker_network_delete,
    docker_ps,
    docker_volume_create,
)
from oprim._exceptions import OprimError
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import PERMISSIONS_BY_ROLE, Permission, require_permission
from aegis.server.models import Role
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/docker", tags=["docker"])

_502 = status.HTTP_502_BAD_GATEWAY


async def _resolve_docker_host(
    conn: asyncpg.Connection, org_id: UUID, node_id: UUID | None
) -> str:
    """Resolve the target Docker daemon for a request.

    node_id=None → the platform's own daemon (settings.docker_host). The REST
    container endpoints previously ignored settings.docker_host AND the node, so
    every action hit oprim's hardcoded local socket; this routes them to the
    selected node's docker_host_url so multi-host control actually works.
    """
    if node_id is None:
        return get_settings().docker_host
    row = await conn.fetchrow(
        "SELECT docker_host_url FROM aegis_nodes WHERE org_id = $1 AND node_id = $2",
        org_id,
        node_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    return row["docker_host_url"] or get_settings().docker_host


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
        items = await asyncio.to_thread(docker_ps, all=all, docker_host=docker_host)
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
            docker_container_inspect, container_id=container, docker_host=docker_host
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
            docker_container_start, container_id=container, docker_host=docker_host
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
            docker_container_stop, container_id=container, docker_host=docker_host
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
            docker_container_restart, container_id=container, docker_host=docker_host
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
            docker_host=docker_host,
        )
        return {"container": container, "lines": [line.model_dump() for line in result]}
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


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
            docker_container_stats, container_id=container, docker_host=docker_host
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


@router.post("/containers/{container}/exec", status_code=status.HTTP_200_OK)
async def exec_container(
    org_id: UUID,
    container: str,
    req: ContainerExecRequest,
    node_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Execute a command in a container."""
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
            docker_host=docker_host,
        )
        return result.model_dump()
    except OprimError as exc:
        raise HTTPException(status_code=_502, detail=str(exc)) from exc


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
    #    hold container-exec permission (TRIGGER_AUTOHEAL, mirrors exec_container).
    #    NOTE: container_name is not yet scoped to org_id (containers are global in
    #    this design); acceptable in self-hosted single-tenant, revisit for multi-tenant.
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
    if Permission.TRIGGER_AUTOHEAL not in PERMISSIONS_BY_ROLE[role]:
        await websocket.close(code=1008)  # insufficient permission for container exec
        return

    await websocket.accept()

    # 2. Establish docker exec interactive session
    import docker
    import docker.errors

    settings = get_settings()
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
                    json.dumps({"type": "output", "data": data.decode("utf-8", errors="replace")})
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


def _read_socket(sock: Any, size: int = 4096) -> bytes | None:
    """Non-blocking socket read. Returns None if no data."""

    try:
        return sock.recv(size)
    except (OSError, BlockingIOError):
        return None
