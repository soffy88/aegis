"""Storage / drive observability API (read-only, R0).

CasaOS-LocalStorage parity — surfaces the host's physical disks/partitions,
USB devices, and per-disk SMART health for the storage dashboard. Read-only:
no mount/unmount/format here (those are R2/R3, gated separately in a later phase).

Runs host probes through the privileged host-shell helper, so — like the
firewall router — reads are gated on INSTALL_APP (operator+) rather than plain
viewer, even though the output itself is benign hardware inventory.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.services import storage as storage_svc
from aegis.server.services.storage import StoragePrimitivesUnavailable

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
