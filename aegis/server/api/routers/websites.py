"""One-click websites — serve a directory (from the file-manager roots) as a
static (nginx) or PHP (php:apache) site in a container on an auto-freed host port.
"""

from __future__ import annotations

import subprocess
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.runtime.config import get_settings

router = APIRouter(prefix="/api/v1/orgs/{org_id}/websites", tags=["websites"])

_LABEL = "aegis.website"
_SLUG = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


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
    """Prepend a Host-matched route on Caddy's main server → the website container.
    HTTPS is provided by the Cloudflare edge once the domain routes through the tunnel."""
    import httpx  # noqa: PLC0415

    route = {
        "@id": f"website-{name}",
        "match": [{"host": [domain]}],
        "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": f"website-{name}:80"}]}],
        "terminal": True,
    }
    r = httpx.put(f"{_CADDY_ADMIN}/config/apps/http/servers/srv0/routes/0", json=route, timeout=8)
    r.raise_for_status()


def _caddy_del_domain(name: str) -> None:
    import httpx  # noqa: PLC0415

    try:
        httpx.delete(f"{_CADDY_ADMIN}/id/website-{name}", timeout=8)
    except Exception:  # noqa: BLE001
        pass


@router.get("")
async def list_websites(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    r = _dcmd(
        [
            "ps",
            "-a",
            "--filter",
            f"label={_LABEL}",
            "--format",
            '{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Label "aegis.website.domain"}}',
        ]
    )
    out = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            out.append(
                {
                    "name": parts[0].removeprefix("website-"),
                    "container": parts[0],
                    "status": parts[1] if len(parts) > 1 else "",
                    "ports": parts[2] if len(parts) > 2 else "",
                    "domain": parts[3] if len(parts) > 3 and parts[3] else None,
                }
            )
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_website(
    org_id: uuid.UUID,
    req: WebsiteRequest,
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
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

    cname = f"website-{req.name}"
    port = _pick_free_host_port(8100, _dh())
    if req.php:
        image, mount = "php:8.3-apache", "/var/www/html"
    else:
        image, mount = "nginx:alpine", "/usr/share/nginx/html"

    _caddy_del_domain(req.name)
    _dcmd(["rm", "-f", cname])
    run = _dcmd(
        [
            "run",
            "-d",
            "--name",
            cname,
            "--restart",
            "unless-stopped",
            "--network",
            _EDGE_NET,
            "--label",
            f"{_LABEL}=true",
            "--label",
            f"{_LABEL}.domain={req.domain or ''}",
            "-v",
            f"{root}:{mount}:ro",
            "-p",
            f"{port}:80",
            image,
        ],
        timeout=60,
    )
    if run.returncode != 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, (run.stderr or "docker run failed")[:300])

    domain_bound = False
    https = None
    if req.domain:
        try:
            _caddy_add_domain(req.name, req.domain)
            domain_bound = True
            https = (
                "HTTPS is served automatically by Cloudflare once this domain's DNS "
                "points at the tunnel and it's added as a tunnel public hostname."
            )
        except Exception as exc:  # noqa: BLE001
            https = f"domain route failed: {exc}"

    return {
        "name": req.name,
        "container": cname,
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
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> None:
    if not _SLUG.match(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid name")
    _caddy_del_domain(name)
    _dcmd(["rm", "-f", f"website-{name}"])
