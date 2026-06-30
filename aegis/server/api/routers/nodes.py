"""Nodes (multi-host) management API."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.models import Node

router = APIRouter(prefix="/api/v1/orgs/{org_id}/nodes", tags=["nodes"])


class NodeRegisterPayload(BaseModel):
    host: str
    node_label: str
    ssh_username: str
    docker_connection_mode: str = "auto"
    key_path: str | None = None
    ssh_port: int = 22
    docker_tcp_port: int | None = None


class NodeHeartbeatPayload(BaseModel):
    agent_token: str
    server_version: str | None = None
    os: str | None = None
    arch: str | None = None
    cpus: int | None = None
    memory_bytes: int | None = None


@router.get("")
async def list_nodes(
    org_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List all nodes in the organization."""
    rows = await conn.fetch("SELECT * FROM aegis_nodes WHERE org_id = $1", org_id)
    return [Node.from_row(row).to_dict() for row in rows]


@router.post("/register")
async def register_node(
    org_id: UUID,
    payload: NodeRegisterPayload,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_PROJECT)),
) -> dict[str, Any]:
    """Register (or re-register) a node and mint its agent token.

    The previous implementation called a non-existent `node_register` omodul with a
    mismatched dispatcher signature and could never succeed. This does a direct
    upsert keyed on (org_id, node_label): a first registration generates an
    agent_token (returned once); re-registration updates connection details and
    refreshes last_seen but keeps the existing token (returns token=None).
    """
    docker_host_url = (
        f"tcp://{payload.host}:{payload.docker_tcp_port}" if payload.docker_tcp_port else None
    )
    new_token = secrets.token_urlsafe(32)
    row = await conn.fetchrow(
        """
        INSERT INTO aegis_nodes
            (org_id, host, node_label, docker_mode, docker_host_url, agent_token, last_seen)
        VALUES ($1, $2, $3, $4, $5, $6, now())
        ON CONFLICT (org_id, node_label) DO UPDATE
            SET host = EXCLUDED.host,
                docker_mode = EXCLUDED.docker_mode,
                docker_host_url = EXCLUDED.docker_host_url,
                last_seen = now()
        RETURNING node_id, agent_token, (xmax = 0) AS inserted
        """,
        org_id,
        payload.host,
        payload.node_label,
        payload.docker_connection_mode,
        docker_host_url,
        new_token,
    )
    inserted = row["inserted"]
    return {
        "node_id": str(row["node_id"]),
        "node_label": payload.node_label,
        # Only reveal the token on first registration; it cannot be retrieved later.
        "agent_token": row["agent_token"] if inserted else None,
        "status": "registered" if inserted else "updated",
    }


@router.post("/{node_id}/heartbeat")
async def node_heartbeat(
    org_id: UUID,
    node_id: UUID,
    payload: NodeHeartbeatPayload,
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> dict[str, Any]:
    """Refresh a node's liveness. Authenticated by the node's agent_token (NOT a
    user JWT) — this is called by the node agent, not the console.

    Without this endpoint last_seen was never updated after registration, so node
    status had no real signal. The token is compared in constant time.
    """
    row = await conn.fetchrow(
        "SELECT agent_token FROM aegis_nodes WHERE org_id = $1 AND node_id = $2",
        org_id,
        node_id,
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    stored = row["agent_token"] or ""
    if not payload.agent_token or not secrets.compare_digest(stored, payload.agent_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid agent token")

    await conn.execute(
        """
        UPDATE aegis_nodes
           SET last_seen = now(),
               server_version = COALESCE($3, server_version),
               os = COALESCE($4, os),
               arch = COALESCE($5, arch),
               cpus = COALESCE($6, cpus),
               memory_bytes = COALESCE($7, memory_bytes)
         WHERE org_id = $1 AND node_id = $2
        """,
        org_id,
        node_id,
        payload.server_version,
        payload.os,
        payload.arch,
        payload.cpus,
        payload.memory_bytes,
    )
    return {"node_id": str(node_id), "status": "online"}


@router.get("/{node_id}")
async def get_node(
    org_id: UUID,
    node_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get detailed information about a single node."""
    row = await conn.fetchrow(
        "SELECT * FROM aegis_nodes WHERE org_id = $1 AND node_id = $2", org_id, node_id
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    return Node.from_row(row).to_dict()


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    org_id: UUID,
    node_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_PROJECT)),
) -> None:
    """Delete a node registration."""
    result = await conn.execute(
        "DELETE FROM aegis_nodes WHERE org_id = $1 AND node_id = $2", org_id, node_id
    )
    if result == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")


@router.get("/{node_id}/containers")
async def list_node_containers(
    org_id: UUID,
    node_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List containers running on a specific node."""
    from oprim import docker_ps

    row = await conn.fetchrow(
        "SELECT docker_host_url FROM aegis_nodes WHERE org_id = $1 AND node_id = $2",
        org_id,
        node_id,
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")

    docker_host = row["docker_host_url"]
    if not docker_host:
        return []

    try:
        items = await asyncio.to_thread(docker_ps, all=True, docker_host=docker_host)
        return [c.model_dump() if hasattr(c, "model_dump") else c for c in items]
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
