"""Remote access API — Dynamic DNS (CasaOS remote-access parity).

DDNS config CRUD + on-demand refresh. Credentials are write-only (stored in the
vault, never returned). Refresh delegates to the 3O ``oprim.ddns_update``
primitive; when the oprim pin predates it, refresh returns 503.

Mesh VPN (Tailscale / ZeroTier) is a separate later slice — those need host
daemons and run through the privileged host-shell helper.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.services import ddns as ddns_svc
from aegis.server.services.ddns import DdnsPrimitiveUnavailable

router = APIRouter(prefix="/api/v1/orgs/{org_id}/remote-access", tags=["remote-access"])


class DdnsCreateRequest(BaseModel):
    provider: str  # duckdns | dyndns2
    hostname: str
    secret: str  # duckdns token OR dyndns2 password (write-only)
    username: str | None = None  # dyndns2 only
    base_url: str | None = None  # dyndns2 provider override


@router.post("/ddns", status_code=status.HTTP_201_CREATED)
async def create_ddns(
    org_id: uuid.UUID,
    req: DdnsCreateRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
) -> dict[str, Any]:
    """Create a DDNS config. admin+ (stores a credential)."""
    try:
        return await ddns_svc.create_config(
            conn,
            org_id=org_id,
            provider=req.provider,
            hostname=req.hostname,
            secret=req.secret,
            username=req.username,
            base_url=req.base_url,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/ddns")
async def list_ddns(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List DDNS configs (credentials never returned)."""
    return await ddns_svc.list_configs(conn, org_id=org_id)


@router.delete("/ddns/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ddns(
    org_id: uuid.UUID,
    config_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_ORG)),
) -> None:
    """Delete a DDNS config and its stored credential. admin+."""
    if not await ddns_svc.delete_config(conn, org_id=org_id, config_id=config_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ddns config not found")


@router.post("/ddns/{config_id}/update")
async def update_ddns(
    org_id: uuid.UUID,
    config_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.TRIGGER_AUTOHEAL)),
) -> dict[str, Any]:
    """Push the current IP to the provider now. operator+."""
    try:
        return await ddns_svc.update_now(conn, org_id=org_id, config_id=config_id)
    except DdnsPrimitiveUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
