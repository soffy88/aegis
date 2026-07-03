"""Scalable logs — proxy to a Grafana Loki instance (LogQL query + retention).

Complements the built-in cross-container grep (no external dep) with a real,
indexed, retained log backend when AEGIS_LOKI_URL points at a Loki deployment
(installable from the App Store).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.runtime.config import get_settings

router = APIRouter(prefix="/api/v1/orgs/{org_id}/loki", tags=["loki"])


def _base() -> str:
    url = (get_settings().loki_url or "").rstrip("/")
    if not url:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Loki is not configured (set AEGIS_LOKI_URL)"
        )
    return url


@router.get("/status")
async def loki_status(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    url = (get_settings().loki_url or "").rstrip("/")
    if not url:
        return {"configured": False}
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{url}/ready")
        return {"configured": True, "url": url, "reachable": r.status_code < 400}
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "url": url, "reachable": False, "detail": str(exc)}


@router.get("/labels")
async def loki_labels(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{_base()}/loki/api/v1/labels")
        return r.json()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/query")
async def loki_query(
    org_id: uuid.UUID,
    query: str = Query(..., description='LogQL, e.g. {container="aegis-backend"}'),
    minutes: int = Query(default=60, ge=1, le=1440),
    limit: int = Query(default=200, ge=1, le=1000),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Proxy a LogQL query_range to Loki and flatten it to log lines."""
    now = time.time_ns()
    start = now - minutes * 60 * 1_000_000_000
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{_base()}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": start,
                    "end": now,
                    "limit": limit,
                    "direction": "backward",
                },
            )
        r.raise_for_status()
        data = r.json().get("data", {})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    lines: list[dict[str, Any]] = []
    for stream in data.get("result", []):
        labels = stream.get("stream", {})
        label = labels.get("container") or labels.get("job") or labels.get("app") or "?"
        for ts, line in stream.get("values", []):
            lines.append({"stream": label, "ts_ns": ts, "message": line})
    lines.sort(key=lambda x: x["ts_ns"], reverse=True)
    return {"total": len(lines), "lines": lines[:limit]}
