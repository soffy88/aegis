"""Service Topology API — visualize service dependencies.

AEGIS_DESIGN v1.1.0 §8
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.service_topology import TopologyBuilder

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/topology", tags=["topology"])


@router.get("")
async def get_topology(
    project_id: UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """获取服务拓扑图。

    Returns: { nodes: [...], edges: [...], has_cycle: bool }
    """
    if not user.orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No org context")

    org_id = user.orgs[0].org_id
    builder = TopologyBuilder(org_id=org_id, project_id=project_id)
    graph = await builder.build()

    # 转换输出
    nodes = [
        {
            "id": str(n.id),
            "name": n.name,
            "kind": n.kind,
            "hostname": n.hostname,
            "port": n.port,
            "labels": n.labels,
            "metadata": n.metadata,
        }
        for n in graph.nodes.values()
    ]

    edges = [
        {
            "id": str(e.id),
            "source": str(e.source.id),
            "target": str(e.target.id),
            "kind": e.kind,
            "weight": e.weight,
        }
        for e in graph.edges
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "has_cycle": graph.has_cycle(),
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


@router.get("/{node_name}/ancestors")
async def get_ancestors(
    node_name: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """获取某节点的所有上游依赖"""
    if not user.orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No org context")

    org_id = user.orgs[0].org_id
    builder = TopologyBuilder(org_id=org_id)
    graph = await builder.build()

    ancestors = graph.get_ancestors(node_name)
    return {"node": node_name, "ancestors": list(ancestors)}


@router.get("/{node_name}/descendants")
async def get_descendants(
    node_name: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> dict:
    """获取某节点的所有下游依赖"""
    if not user.orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No org context")

    org_id = user.orgs[0].org_id
    builder = TopologyBuilder(org_id=org_id)
    graph = await builder.build()

    descendants = graph.get_descendants(node_name)
    return {"node": node_name, "descendants": list(descendants)}