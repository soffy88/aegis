"""Nodes (multi-host) management API."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.dispatch import OmodulDispatcher
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
    user: UserContext = Depends(require_permission(Permission.MODIFY_PROJECT)),
) -> dict[str, Any]:
    """Register a new node via omodul.node_register."""
    dispatcher = OmodulDispatcher(
        omodul_name="node_register",
        user_id=str(user.user_id),
        org_id=str(org_id),
    )

    try:
        # Note: mapping logic depends on omodul.node_register's actual Config/Input
        # We assume dispatcher._resolve_class handles finding them.
        result = await dispatcher.invoke(
            config_kwargs={},
            input_kwargs=payload.model_dump(),
        )
        return result
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


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
