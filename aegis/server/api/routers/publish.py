"""Publish an already-running internal service to the public internet.

Distinct from `websites.py` (which creates NEW site containers from a directory
or template): this wires up a service that already exists — pick a running
container:port, assign a domain, and this router does the rest: a Caddy
host-matched route on the same internal listener aegis's own console/API use
(reused as-is from edge/caddy.py, no new Caddy plumbing) plus an ingress rule +
DNS record on the *same* Cloudflare Tunnel aegis-cloudflared already runs.

Requires an org secret named "cloudflare_api_token" (Cloudflare API token with
Tunnel:Edit + DNS:Edit scope on the target zone — see /orgs/{org_id}/secrets)
and AEGIS_CLOUDFLARED_TOKEN (same token aegis-cloudflared authenticates with).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.edge.caddy import get_caddy_edge
from aegis.server.edge.caddy import org_route_id as _org_route_id
from aegis.server.persistence.audit import record_audit
from aegis.server.runtime.config import get_settings
from aegis.server.services import cloudflare
from aegis.server.services.secrets_vault import reveal_secret

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/publish", tags=["publish"])

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,58}$")
_UPSTREAM = re.compile(r"^[a-zA-Z0-9_.-]+:\d{1,5}$")
_503 = status.HTTP_503_SERVICE_UNAVAILABLE
_400 = status.HTTP_400_BAD_REQUEST


class PublishRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    upstream: str = Field(..., min_length=1, description='"container:port"')
    domain: str = Field(..., min_length=1)


def _root_domain(domain: str) -> str:
    """Best-effort eTLD+1 extraction (good enough for the domains this feature
    actually targets — two-label roots like kanpan.co). Custom multi-part TLDs
    would need a public-suffix list; not needed for this use case."""
    parts = domain.strip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def _edge():
    edge = get_caddy_edge()
    if edge is None:
        raise HTTPException(_503, "CaddyEdge not initialized — check caddy_admin_url config")
    return edge


async def _cf_token(conn: asyncpg.Connection, org_id: uuid.UUID) -> str:
    token = await reveal_secret(conn, org_id=org_id, name="cloudflare_api_token")
    if not token:
        raise HTTPException(
            _400,
            "no cloudflare_api_token secret set for this org — add one via "
            "Settings before publishing (needs Tunnel:Edit + DNS:Edit scope)",
        )
    return token


def _tunnel_identity() -> tuple[str, str]:
    cfg = get_settings()
    if not cfg.cloudflared_token:
        raise HTTPException(_503, "AEGIS_CLOUDFLARED_TOKEN not configured — Publish unavailable")
    return cloudflare.parse_tunnel_token(cfg.cloudflared_token)


@router.get("")
async def list_published(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, name, upstream, domain, status, last_error, created_at"
        " FROM published_services WHERE org_id = $1 ORDER BY created_at DESC",
        org_id,
    )
    return [dict(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def publish_service(
    org_id: uuid.UUID,
    req: PublishRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> dict[str, Any]:
    if not _NAME.match(req.name):
        raise HTTPException(_400, "name must be lowercase alnum/hyphen")
    if not _UPSTREAM.match(req.upstream):
        raise HTTPException(_400, 'upstream must look like "container:port"')

    cf_token = await _cf_token(conn, org_id)
    account_id, tunnel_id = _tunnel_identity()
    edge = _edge()
    route_id = _org_route_id(org_id, req.domain)

    try:
        await asyncio.to_thread(edge.add_route, req.domain, req.upstream, route_id=route_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(_503, f"caddy route failed: {exc}") from exc

    try:
        service = f"http://{req.upstream}"
        await cloudflare.add_public_hostname(cf_token, account_id, tunnel_id, req.domain, service)
        zone_id = await cloudflare.get_zone_id(cf_token, _root_domain(req.domain))
        record_id = await cloudflare.ensure_dns_record(cf_token, zone_id, req.domain, tunnel_id)
    except Exception as exc:  # noqa: BLE001
        with __import__("contextlib").suppress(Exception):
            await asyncio.to_thread(edge.remove_route, route_id)
        raise HTTPException(
            _503, f"cloudflare setup failed (rolled back caddy route): {exc}"
        ) from exc

    await conn.execute(
        """
        INSERT INTO published_services
            (org_id, name, upstream, domain, caddy_route_id, cf_dns_record_id, status)
        VALUES ($1, $2, $3, $4, $5, $6, 'active')
        ON CONFLICT (org_id, domain) DO UPDATE SET
            name = EXCLUDED.name, upstream = EXCLUDED.upstream,
            caddy_route_id = EXCLUDED.caddy_route_id, cf_dns_record_id = EXCLUDED.cf_dns_record_id,
            status = 'active', last_error = NULL
        """,
        org_id,
        req.name,
        req.upstream,
        req.domain,
        route_id,
        record_id,
    )
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="publish.created",
        target_type="published_service",
        target_id=req.name,
        metadata={"upstream": req.upstream, "domain": req.domain},
    )
    return {"name": req.name, "upstream": req.upstream, "domain": req.domain, "status": "active"}


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unpublish_service(
    org_id: uuid.UUID,
    item_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIGURE_NOTIFY)),
) -> None:
    row = await conn.fetchrow(
        "SELECT name, domain, caddy_route_id, cf_dns_record_id FROM published_services"
        " WHERE id = $1 AND org_id = $2",
        item_id,
        org_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    # Best-effort cleanup: an entry with a partially-broken external state must
    # still be deletable, so each external call is caught+logged individually
    # rather than aborting the whole delete (mirrors websites.py's _caddy_del_domain).
    try:
        cf_token = await _cf_token(conn, org_id)
        account_id, tunnel_id = _tunnel_identity()
        await cloudflare.remove_public_hostname(cf_token, account_id, tunnel_id, row["domain"])
        if row["cf_dns_record_id"]:
            zone_id = await cloudflare.get_zone_id(cf_token, _root_domain(row["domain"]))
            await cloudflare.delete_dns_record(cf_token, zone_id, row["cf_dns_record_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("publish_delete_cloudflare_cleanup_failed name=%s: %s", row["name"], exc)

    with __import__("contextlib").suppress(Exception):
        await asyncio.to_thread(_edge().remove_route, row["caddy_route_id"])

    await conn.execute(
        "DELETE FROM published_services WHERE id = $1 AND org_id = $2", item_id, org_id
    )
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="publish.deleted",
        target_type="published_service",
        target_id=row["name"],
    )
