"""Edge route management API — wraps CaddyEdge for Caddy admin operations."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.edge.caddy import CaddyEdge, get_caddy_edge

router = APIRouter(prefix="/api/v1/orgs/{org_id}/edge/routes", tags=["edge"])

_503 = status.HTTP_503_SERVICE_UNAVAILABLE


def _edge() -> CaddyEdge:
    edge = get_caddy_edge()
    if edge is None:
        raise HTTPException(
            status_code=_503,
            detail="CaddyEdge not initialized — check caddy_admin_url config",
        )
    return edge


class RouteCreateRequest(BaseModel):
    domain: str
    upstream: str
    route_id: str | None = None
    service_url: str = ""


@router.get("")
async def list_routes(
    org_id: UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List all Caddy routes. viewer+ can read."""
    edge = _edge()
    try:
        return await asyncio.to_thread(edge.list_routes)
    except Exception as exc:
        raise HTTPException(status_code=_503, detail=str(exc)) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_route(
    org_id: UUID,
    req: RouteCreateRequest,
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict[str, Any]:
    """Add a Caddy route. member+ required."""
    edge = _edge()
    try:
        return await asyncio.to_thread(
            edge.add_route,
            req.domain,
            req.upstream,
            route_id=req.route_id,
            service_url=req.service_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=_503, detail=str(exc)) from exc


@router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_route(
    org_id: UUID,
    route_id: str,
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> None:
    """Remove a Caddy route by ID. member+ required."""
    edge = _edge()
    try:
        await asyncio.to_thread(edge.remove_route, route_id)
    except Exception as exc:
        raise HTTPException(status_code=_503, detail=str(exc)) from exc
