"""One-click **managed** websites (ADR-004) — serve a directory (from the
file-manager roots) as a static (nginx) or PHP (php:apache) site in a container on
an auto-freed host port.

Unlike the original build-and-forget version, sites are first-class managed assets:
each is a row in `sites`, its container is labelled `aegis.managed=true`, its health
is probed by the reconcile loop, and create/delete are written to the audit log.
Container lifecycle goes through the `obase.docker` primitives (not raw `docker run`);
removal still shells out because obase exposes no container-remove primitive.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence.audit import record_audit
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/websites", tags=["websites"])

_LABEL = "aegis.website"
_SLUG = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

# runtime → (base image, document-root mount). P2 (ADR-004) adds node / nextjs-oui.
_RUNTIMES: dict[str, tuple[str, str]] = {
    "static": ("nginx:alpine", "/usr/share/nginx/html"),
    "php": ("php:8.3-apache", "/var/www/html"),
}


def _runtime_image_mount(runtime: str) -> tuple[str, str]:
    try:
        return _RUNTIMES[runtime]
    except KeyError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unsupported runtime: {runtime}"
        ) from None


def _dh() -> str:
    return get_settings().docker_host


def _dcmd(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", "-H", _dh(), *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class WebsiteRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    root_dir: str = Field(..., min_length=1)
    php: bool = False
    domain: str | None = None


_CADDY_ADMIN = "http://aegis-caddy:2019"
_EDGE_NET = "helios-net"  # Caddy's network — website containers join it for name routing


def _caddy_add_domain(name: str, domain: str) -> None:
    """Bind *domain* to the website container on Caddy's :443 'sites' server so
    Caddy serves it and auto-issues a Let's Encrypt cert (the domain's DNS must
    point at this host with 80/443 open)."""
    import httpx  # noqa: PLC0415

    route = {
        "@id": f"website-{name}",
        "match": [{"host": [domain]}],
        "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": f"website-{name}:80"}]}],
        "terminal": True,
    }
    # Idempotent: drop any existing route with this @id first (so reconcile /
    # re-create doesn't stack duplicates).
    with __import__("contextlib").suppress(Exception):
        httpx.delete(f"{_CADDY_ADMIN}/id/website-{name}", timeout=8)
    base = f"{_CADDY_ADMIN}/config/apps/http/servers/sites"
    r = httpx.get(base, timeout=8)
    if r.status_code != 200 or not r.json():
        # Create the sites server (Caddy adds the :80 ACME listener itself).
        httpx.put(base, json={"listen": [":443"], "routes": [route]}, timeout=8).raise_for_status()
    else:
        httpx.put(f"{base}/routes/0", json=route, timeout=8).raise_for_status()


def _caddy_del_domain(name: str) -> None:
    import contextlib  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    with contextlib.suppress(Exception):
        httpx.delete(f"{_CADDY_ADMIN}/id/website-{name}", timeout=8)


@router.get("")
async def list_websites(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT name, container, runtime, host_port, domain, status, last_up,"
        " last_checked_at, last_error FROM sites WHERE org_id = $1 ORDER BY created_at DESC",
        org_id,
    )
    out = []
    for r in rows:
        d = dict(r)
        # Keep the legacy `ports` string the current console table renders.
        d["port"] = d.get("host_port")
        d["ports"] = f"{d['host_port']}->80" if d.get("host_port") else ""
        out.append(d)
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_website(
    org_id: uuid.UUID,
    req: WebsiteRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    from obase.docker import docker_container_create, docker_container_start  # noqa: PLC0415

    from aegis.server.api.routers.apps import _pick_free_host_port  # noqa: PLC0415
    from aegis.server.services.files import _safe  # noqa: PLC0415

    if not _SLUG.match(req.name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name must be lowercase alnum/hyphen")
    try:
        root = _safe(req.root_dir)  # enforce it's inside a file-manager root
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"root_dir invalid: {exc}") from exc
    if not root.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "root_dir is not a directory")

    runtime = "php" if req.php else "static"
    image, mount = _runtime_image_mount(runtime)
    cname = f"website-{req.name}"
    dh = _dh()
    port = _pick_free_host_port(8100, dh)

    # Idempotent (re-)create: drop any stale Caddy route + container first. obase
    # exposes no container-remove primitive, so removal shells out (see module docstring).
    _caddy_del_domain(req.name)
    _dcmd(["rm", "-f", cname])

    labels = {
        _LABEL: "true",
        f"{_LABEL}.domain": req.domain or "",
        "aegis.managed": "true",  # enrol into the managed-container view / autoheal scope
        "aegis.site": req.name,
        "aegis.org": str(org_id),
    }
    try:
        await asyncio.to_thread(
            docker_container_create,
            image=image,
            name=cname,
            labels=labels,
            restart_policy="unless-stopped",
            network=_EDGE_NET,
            ports={"80/tcp": port},
            volumes={str(root): {"bind": mount, "mode": "ro"}},
            docker_host=dh,
        )
        await asyncio.to_thread(docker_container_start, container_id=cname, docker_host=dh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"container create failed: {exc}"[:300]
        ) from exc

    domain_bound = False
    https = None
    if req.domain:
        try:
            _caddy_add_domain(req.name, req.domain)
            domain_bound = True
            https = (
                "Caddy auto-issues a Let's Encrypt certificate — point the domain's "
                "A/AAAA record at this host with ports 80/443 open."
            )
        except Exception as exc:  # noqa: BLE001
            https = f"domain route failed: {exc}"

    await conn.execute(
        """
        INSERT INTO sites
            (org_id, name, runtime, root_dir, container, image, host_port, domain, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'running')
        ON CONFLICT (org_id, name) DO UPDATE SET
            runtime = EXCLUDED.runtime, root_dir = EXCLUDED.root_dir,
            container = EXCLUDED.container, image = EXCLUDED.image,
            host_port = EXCLUDED.host_port, domain = EXCLUDED.domain,
            status = 'running', last_error = NULL
        """,
        org_id,
        req.name,
        runtime,
        str(root),
        cname,
        image,
        port,
        req.domain,
    )
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="site.created",
        target_type="site",
        target_id=req.name,
        metadata={"runtime": runtime, "domain": req.domain, "port": port},
    )

    return {
        "name": req.name,
        "container": cname,
        "runtime": runtime,
        "port": port,
        "url": f"http://<host>:{port}",
        "domain": req.domain,
        "domain_bound": domain_bound,
        "https": https,
    }


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_website(
    org_id: uuid.UUID,
    name: str,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> None:
    if not _SLUG.match(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid name")
    _caddy_del_domain(name)
    _dcmd(["rm", "-f", f"website-{name}"])
    await conn.execute("DELETE FROM sites WHERE org_id = $1 AND name = $2", org_id, name)
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="site.deleted",
        target_type="site",
        target_id=name,
    )


def _list_website_domains() -> list[tuple[str, str]]:
    """[(name, domain), ...] for running website containers that have a domain."""
    r = _dcmd(
        [
            "ps",
            "--filter",
            f"label={_LABEL}",
            "--format",
            '{{.Names}}\t{{.Label "aegis.website.domain"}}',
        ]
    )
    out: list[tuple[str, str]] = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            out.append((parts[0].removeprefix("website-"), parts[1]))
    return out


def reconcile_caddy_routes() -> int:
    """Re-apply Caddy Host routes for every website container that declares a
    domain. Idempotent — safe to run repeatedly. Returns how many were applied."""
    n = 0
    for name, domain in _list_website_domains():
        try:
            _caddy_add_domain(name, domain)
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("website route reconcile failed name=%s: %s", name, exc)
    return n


async def _probe_sites() -> int:
    """Inspect every managed site container and record health into `sites`
    (last_up / status / last_checked_at / last_error). Returns rows updated.

    This is the Monitor half of ADR-004's closed loop: a site that dies flips to
    an observable status here; deep autoheal (probe→alert→plugin) is the L3 follow-up.
    """
    from obase.docker import docker_container_inspect  # noqa: PLC0415

    from aegis.server.persistence.db import get_pool  # noqa: PLC0415

    dh = _dh()
    updated = 0
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT id, container FROM sites")
        for r in rows:
            up = False
            err: str | None = None
            st = "stopped"
            try:
                info = await asyncio.to_thread(
                    docker_container_inspect, container_id=r["container"], docker_host=dh
                )
                running = info.state == "running"
                if info.health == "unhealthy":
                    up, st = False, "unhealthy"
                elif running:
                    up, st = True, "running"
                else:
                    up, st = False, (info.state or "stopped")
            except Exception as exc:  # noqa: BLE001 — missing / unreachable container
                up, st, err = False, "error", str(exc)[:300]
            await conn.execute(
                "UPDATE sites SET last_up = $1, status = $2, last_checked_at = now(),"
                " last_error = $3 WHERE id = $4",
                up,
                st,
                err,
                r["id"],
            )
            updated += 1
    return updated


async def website_route_reconcile_loop(interval_sec: int = 60) -> None:
    """Periodically (a) reconcile website Caddy routes so they survive a Caddy
    restart, and (b) probe site health into `sites` (ADR-004). Both idempotent."""
    while True:
        try:
            applied = await asyncio.to_thread(reconcile_caddy_routes)
            if applied:
                log.debug("reconciled %d website Caddy routes", applied)
        except Exception as exc:  # noqa: BLE001
            log.warning("website reconcile loop error: %s", exc)
        try:
            await _probe_sites()
        except Exception as exc:  # noqa: BLE001
            log.warning("website probe loop error: %s", exc)
        await asyncio.sleep(interval_sec)
