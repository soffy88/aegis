"""Storage / drive observability + management API.

CasaOS-LocalStorage parity. Reads (R0) surface disks/partitions, USB devices,
and per-disk SMART health, gated on INSTALL_APP (operator+) like the firewall
router. Mutations — mount/unmount (R2) and format/host-power (R3) — are
owner-only, dry_run by default, and delegated to
:mod:`aegis.server.services.storage_ops` which enforces the guardrails.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, Role, require_min_role, require_permission
from aegis.server.services import storage as storage_svc
from aegis.server.services import storage_ops
from aegis.server.services.storage import StoragePrimitivesUnavailable
from aegis.server.services.storage_ops import StorageOpBlocked

router = APIRouter(prefix="/api/v1/orgs/{org_id}/storage", tags=["storage"])


def _run(fn: Any, **kwargs: Any) -> Any:
    """Call a storage-service fn, mapping its exceptions to HTTP codes."""
    try:
        return fn(**kwargs)
    except StoragePrimitivesUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — host probe failure → bad gateway
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"host storage probe failed: {e}") from e


@router.get("")
async def overview(
    org_id: uuid.UUID,
    smart: bool = Query(default=True, description="Include per-disk SMART health"),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Storage dashboard aggregate: disks (+SMART) + USB devices."""
    return _run(storage_svc.storage_overview, include_smart=smart)


@router.get("/devices")
async def devices(
    org_id: uuid.UUID,
    disks_only: bool = Query(default=False),
    include_loop: bool = Query(default=False),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Enumerate host block devices (disks + partitions)."""
    return _run(storage_svc.list_block_devices, disks_only=disks_only, include_loop=include_loop)


@router.get("/usb")
async def usb(
    org_id: uuid.UUID,
    include_root_hubs: bool = Query(default=False),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Enumerate host USB devices."""
    return _run(storage_svc.list_usb_devices, include_root_hubs=include_root_hubs)


@router.get("/smart")
async def smart(
    org_id: uuid.UUID,
    device: str = Query(..., description="Block device path, e.g. /dev/sda"),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Read SMART health for a single device."""
    return _run(storage_svc.probe_smart, device=device)


# ── Mutations (owner-only, dry_run by default) ───────────────────────────────


class MountRequest(BaseModel):
    device: str
    target: str
    fstype: str | None = None
    dry_run: bool = True


class UnmountRequest(BaseModel):
    target: str
    dry_run: bool = True


class FormatRequest(BaseModel):
    device: str
    fstype: str
    confirm: str  # must echo the device path
    dry_run: bool = True


class PowerRequest(BaseModel):
    action: str  # reboot | poweroff
    confirm: str  # must echo the action
    dry_run: bool = True


async def _mutate(coro: Any) -> Any:
    """Await a storage_ops coroutine, mapping guardrail/primitive errors to HTTP."""
    try:
        return await coro
    except StorageOpBlocked as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except StoragePrimitivesUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e


@router.post("/mount")
async def mount_device(
    org_id: uuid.UUID,
    req: MountRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_min_role(Role.OWNER)),
) -> dict[str, Any]:
    """Mount a block device under /mnt or /media (R2). owner-only, dry_run default."""
    return await _mutate(
        storage_ops.mount(
            conn,
            org_id=org_id,
            actor=user.user_id,
            device=req.device,
            target=req.target,
            fstype=req.fstype,
            dry_run=req.dry_run,
        )
    )


@router.post("/unmount")
async def unmount_path(
    org_id: uuid.UUID,
    req: UnmountRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_min_role(Role.OWNER)),
) -> dict[str, Any]:
    """Unmount a path under /mnt or /media (R2). owner-only, dry_run default."""
    return await _mutate(
        storage_ops.unmount(
            conn, org_id=org_id, actor=user.user_id, target=req.target, dry_run=req.dry_run
        )
    )


@router.post("/format")
async def format_device(
    org_id: uuid.UUID,
    req: FormatRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_min_role(Role.OWNER)),
) -> dict[str, Any]:
    """Create a filesystem on a device (R3, DESTRUCTIVE). owner-only, dry_run default."""
    return await _mutate(
        storage_ops.format_device(
            conn,
            org_id=org_id,
            actor=user.user_id,
            device=req.device,
            fstype=req.fstype,
            confirm=req.confirm,
            dry_run=req.dry_run,
        )
    )


@router.post("/power")
async def host_power(
    org_id: uuid.UUID,
    req: PowerRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_min_role(Role.OWNER)),
) -> dict[str, Any]:
    """Reboot or power off the host (R3). owner-only, dry_run default."""
    return await _mutate(
        storage_ops.host_power(
            conn,
            org_id=org_id,
            actor=user.user_id,
            action=req.action,
            confirm=req.confirm,
            dry_run=req.dry_run,
        )
    )
