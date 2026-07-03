"""Host firewall management (ufw) via the privileged host-shell helper.

Runs `ufw` on the host (chroot /host) through the aegis-host-shell container —
the same privileged helper the host terminal uses. Powerful; gated on INSTALL_APP.
Falls back to reporting iptables when ufw is absent.
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

router = APIRouter(prefix="/api/v1/orgs/{org_id}/firewall", tags=["firewall"])

_HELPER = "aegis-host-shell"


def _ensure_helper(dh: str) -> None:
    ps = subprocess.run(  # noqa: S603
        ["docker", "-H", dh, "ps", "-q", "-f", f"name=^{_HELPER}$"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if ps.stdout.strip():
        return
    subprocess.run(["docker", "-H", dh, "rm", "-f", _HELPER], capture_output=True, check=False)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "-H",
            dh,
            "run",
            "-d",
            "--name",
            _HELPER,
            "--privileged",
            "--pid=host",
            "--network=host",
            "-v",
            "/:/host",
            "--restart",
            "unless-stopped",
            "alpine:latest",
            "sleep",
            "infinity",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def _host(cmd: str) -> tuple[int, str]:
    dh = get_settings().docker_host
    try:
        _ensure_helper(dh)
        r = subprocess.run(  # noqa: S603
            ["docker", "-H", dh, "exec", _HELPER, "chroot", "/host", "bash", "-lc", cmd],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"host exec failed: {exc}") from exc


class RuleRequest(BaseModel):
    port: int = Field(..., ge=1, le=65535)
    protocol: str = Field(default="tcp", pattern="^(tcp|udp)$")
    action: str = Field(default="allow", pattern="^(allow|deny)$")


@router.get("")
async def firewall_status(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Report ufw status + numbered rules (or iptables when ufw is absent)."""
    code, out = _host("command -v ufw >/dev/null 2>&1 && echo HAS_UFW || echo NO_UFW")
    has_ufw = "HAS_UFW" in out
    if has_ufw:
        _, rules = _host("ufw status numbered 2>&1 || true")
        return {"backend": "ufw", "raw": rules}
    _, rules = _host("iptables -L INPUT -n --line-numbers 2>&1 | head -80 || true")
    return {"backend": "iptables", "raw": rules, "note": "ufw not installed on host"}


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def add_rule(
    org_id: uuid.UUID,
    req: RuleRequest,
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, str]:
    code, out = _host(f"ufw --force {req.action} {req.port}/{req.protocol} 2>&1")
    if code != 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, out[:300] or "ufw command failed")
    return {"status": "applied", "detail": out[:300]}


@router.delete("/rules/{num}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    org_id: uuid.UUID,
    num: int,
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> None:
    code, out = _host(f"ufw --force delete {int(num)} 2>&1")
    if code != 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, out[:300] or "ufw delete failed")
