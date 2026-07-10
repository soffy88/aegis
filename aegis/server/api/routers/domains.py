"""Domain registration API — creates real Caddy routes via CaddyEdge.

Converged with the /edge/routes path (STATUS #18): registering a domain here adds
an org-namespaced Caddy route (auto-HTTPS via ACME) using the same route-id scheme
as edge.py, instead of the old best-effort forward to a phantom aegis-edge service.
DNS record hosting stays external (point the domain's DNS at the tunnel/host at your
registrar); Caddy then provisions the TLS cert automatically once DNS resolves.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import urlparse

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.edge.caddy import get_caddy_edge, org_route_id
from aegis.server.repositories.project_repo import ProjectRepository

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/domains", tags=["domains"])


class DomainRegisterRequest(BaseModel):
    domain: str
    target_url: str
    tls_mode: str = "auto"


def _upstream_from_target(target_url: str) -> tuple[str, str]:
    """Split a target like 'http://host:3000' or 'host:3000' into
    (dial_address, service_url) for CaddyEdge.add_route."""
    parsed = urlparse(target_url if "//" in target_url else f"//{target_url}")
    dial = parsed.netloc or target_url
    service_url = target_url if parsed.scheme else f"http://{dial}"
    return dial, service_url


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


def _probe_cert(domain: str) -> dict[str, Any]:
    """Fetch and parse the live TLS certificate served for *domain* on :443.

    Reads the presented cert without verifying it (so expired / self-signed
    certs still report their status) and returns issuer / expiry / days_left.
    """
    import datetime as _dt  # noqa: PLC0415
    import socket  # noqa: PLC0415
    import ssl  # noqa: PLC0415

    from cryptography import x509  # noqa: PLC0415

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((domain, 443), timeout=6) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                der = ssock.getpeercert(binary_form=True)
        cert = x509.load_der_x509_certificate(der)  # type: ignore[arg-type]
        not_after = cert.not_valid_after_utc
        days_left = (not_after - _dt.datetime.now(_dt.UTC)).days
        issuer = next(
            (a.value for a in cert.issuer if a.oid == x509.NameOID.ORGANIZATION_NAME), ""
        ) or next((a.value for a in cert.issuer if a.oid == x509.NameOID.COMMON_NAME), "")
        return {
            "domain": domain,
            "reachable": True,
            "issuer": issuer,
            "not_after": not_after.isoformat(),
            "days_left": days_left,
            "expiring_soon": days_left < 21,
            "expired": days_left < 0,
        }
    except Exception as exc:  # noqa: BLE001 — probe is best-effort
        return {"domain": domain, "reachable": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/certificates")
async def list_certificates(
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """TLS certificate status for each registered domain — probes the live cert
    served on :443 and reports issuer, expiry and days remaining."""
    rows = await conn.fetch(
        """
        SELECT domain FROM domains
         WHERE org_id = $1 AND ($2::uuid IS NULL OR project_id = $2) AND tls_enabled
         ORDER BY domain
        """,
        org_id,
        project_id,
    )
    return await asyncio.gather(*(asyncio.to_thread(_probe_cert, r["domain"]) for r in rows))


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

    # Create the real Caddy route (auto-HTTPS via ACME). Best-effort so a dev host
    # without a running Caddy still records the domain; the response reports the
    # route status so callers can surface a failure.
    route_id = org_route_id(org_id, req.domain)
    upstream, service_url = _upstream_from_target(req.target_url)
    edge_ok = False
    edge_error: str | None = None
    edge = get_caddy_edge()
    if edge is None:
        edge_error = "CaddyEdge not initialized (caddy_admin_url unset?)"
        log.warning("domain route not created: %s", edge_error)
    else:
        try:
            await asyncio.to_thread(
                edge.add_route,
                req.domain,
                upstream,
                route_id=route_id,
                service_url=service_url,
            )
            edge_ok = True
        except Exception as exc:  # noqa: BLE001 — best-effort; DB row still recorded
            edge_error = f"caddy route add failed: {exc}"
            log.warning("domain route creation failed for %s: %s", req.domain, exc)

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
        "route_id": route_id,
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

    # Remove the Caddy route too (best-effort — the DB row is already gone).
    edge = get_caddy_edge()
    if edge is not None:
        try:
            await asyncio.to_thread(edge.remove_route, org_route_id(org_id, domain))
        except Exception as exc:  # noqa: BLE001
            log.warning("caddy route removal failed for %s: %s", domain, exc)
