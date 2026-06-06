"""Edge route management API — wraps CaddyEdge for Caddy admin operations."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.edge.caddy import CaddyEdge, get_caddy_edge

router = APIRouter(prefix="/api/v1/orgs/{org_id}/edge/routes", tags=["edge"])

_503 = status.HTTP_503_SERVICE_UNAVAILABLE
_403 = status.HTTP_403_FORBIDDEN


def _edge() -> CaddyEdge:
    edge = get_caddy_edge()
    if edge is None:
        raise HTTPException(
            status_code=_503,
            detail="CaddyEdge not initialized — check caddy_admin_url config",
        )
    return edge


def _org_prefix(org_id: UUID) -> str:
    """Org-scoped prefix for all Caddy route IDs owned by this org."""
    return f"aegis-org-{org_id}-"


def _org_route_id(org_id: UUID, domain: str) -> str:
    """Generate a deterministic, org-namespaced route ID from a domain."""
    slug = re.sub(r"[^a-z0-9]+", "-", domain.lower()).strip("-")
    return f"{_org_prefix(org_id)}{slug}"


class RouteCreateRequest(BaseModel):
    domain: str
    upstream: str
    service_url: str = ""


@router.get("")
async def list_routes(
    org_id: UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List Caddy routes owned by this org. viewer+ can read."""
    edge = _edge()
    prefix = _org_prefix(org_id)
    try:
        all_routes = await asyncio.to_thread(edge.list_routes)
    except Exception as exc:
        raise HTTPException(status_code=_503, detail=str(exc)) from exc
    return [r for r in all_routes if str(r.get("@id", "")).startswith(prefix)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_route(
    org_id: UUID,
    req: RouteCreateRequest,
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict[str, Any]:
    """Add a Caddy route namespaced to this org. member+ required."""
    edge = _edge()
    route_id = _org_route_id(org_id, req.domain)
    try:
        return await asyncio.to_thread(
            edge.add_route,
            req.domain,
            req.upstream,
            route_id=route_id,
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
    """Remove a Caddy route. Rejects route_ids that belong to a different org."""
    if not route_id.startswith(_org_prefix(org_id)):
        raise HTTPException(
            status_code=_403,
            detail="route_id does not belong to this org",
        )
    edge = _edge()
    try:
        await asyncio.to_thread(edge.remove_route, route_id)
    except Exception as exc:
        raise HTTPException(status_code=_503, detail=str(exc)) from exc
