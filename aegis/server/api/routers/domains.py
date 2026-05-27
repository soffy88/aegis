"""Domain registration API — forwards to aegis-edge."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.repositories.project_repo import ProjectRepository

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/domains", tags=["domains"])

_EDGE_URL = "http://localhost:8081"
_EDGE_TIMEOUT = 10.0


class DomainRegisterRequest(BaseModel):
    domain: str
    target_url: str
    tls_mode: str = "auto"


@router.get("")
async def list_domains(
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List domains. project_id=None returns all in this org."""
    rows = await conn.fetch(
        """
        SELECT domain, target_url, tls_enabled, created_at
          FROM domains
         WHERE org_id = $1 AND ($2::uuid IS NULL OR project_id = $2)
         ORDER BY created_at DESC
        """,
        org_id,
        project_id,
    )
    return [dict(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_domain(
    org_id: uuid.UUID,
    req: DomainRegisterRequest,
    project_id: uuid.UUID = Query(..., description="Project this domain belongs to"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict[str, Any]:
    """Register a domain. member+ required."""
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found in this org"
        )

    # Forward to aegis-edge (best-effort — dev may not have edge running)
    edge_ok = False
    edge_error: str | None = None
    try:
        async with httpx.AsyncClient(timeout=_EDGE_TIMEOUT) as client:
            r = await client.post(
                f"{_EDGE_URL}/api/v1/domains",
                json={
                    "domain": req.domain,
                    "target_url": req.target_url,
                    "tls_mode": req.tls_mode,
                },
            )
        if r.is_success:
            edge_ok = True
        else:
            edge_error = f"edge HTTP {r.status_code}: {r.text[:200]}"
            log.warning("aegis-edge domain registration failed: %s", edge_error)
    except httpx.RequestError as exc:
        edge_error = f"edge unreachable: {exc}"
        log.warning("aegis-edge not reachable (dev mode?): %s", exc)

    await conn.execute(
        """
        INSERT INTO domains (domain, org_id, project_id, target_url, tls_enabled)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (domain) DO UPDATE
            SET target_url = EXCLUDED.target_url,
                tls_enabled = EXCLUDED.tls_enabled
        """,
        req.domain,
        org_id,
        project_id,
        req.target_url,
        req.tls_mode != "off",
    )

    return {
        "domain": req.domain,
        "target_url": req.target_url,
        "edge_registered": edge_ok,
        "edge_error": edge_error,
    }


@router.delete("/{domain}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_domain(
    org_id: uuid.UUID,
    domain: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> None:
    """Delete a domain. member+ required."""
    result = await conn.execute(
        "DELETE FROM domains WHERE domain = $1 AND org_id = $2",
        domain,
        org_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")
