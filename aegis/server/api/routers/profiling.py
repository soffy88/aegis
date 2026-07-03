"""Continuous profiling — surfaces a configured Grafana Pyroscope instance
(install from the App Store, set AEGIS_PYROSCOPE_URL). Profiling requires the
Pyroscope agent/SDK in the target app; this exposes the flamegraph UI + apps."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.runtime.config import get_settings

router = APIRouter(prefix="/api/v1/orgs/{org_id}/profiling", tags=["profiling"])


@router.get("/status")
async def profiling_status(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    url = (get_settings().pyroscope_url or "").rstrip("/")
    if not url:
        return {"configured": False}
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{url}/ready")
        return {"configured": True, "url": url, "reachable": r.status_code < 400}
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "url": url, "reachable": False, "detail": str(exc)}


@router.get("/apps")
async def profiling_apps(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """List profiled applications from Pyroscope.

    Pyroscope 1.x dropped the 0.x ``/pyroscope/api/apps`` route; apps are now the
    values of the ``service_name`` label, exposed via the Connect QuerierService.
    """
    url = (get_settings().pyroscope_url or "").rstrip("/")
    if not url:
        return {"configured": False, "apps": []}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(
                f"{url}/querier.v1.QuerierService/LabelValues",
                json={"name": "service_name", "start": 0, "end": 0},
                headers={"Content-Type": "application/json"},
            )
        apps = r.json().get("names", []) if r.status_code == 200 else []
        return {"configured": True, "apps": apps}
    except Exception:  # noqa: BLE001
        return {"configured": True, "apps": []}
