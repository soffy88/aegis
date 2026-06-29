"""Health check + readiness endpoints (公开, 不带 org)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from aegis.server.persistence import get_pool

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("")
async def health() -> dict[str, Any]:
    """Liveness check — does not touch DB. Public, no auth."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, Any]:
    """Readiness check — verifies the DB pool can serve a query.

    Returns 503 when Postgres is unreachable so an orchestrator can gate traffic
    away from this replica. Liveness (`/health`) stays dependency-free so a DB
    blip doesn't trigger container restarts.
    """
    try:
        async with get_pool().acquire() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"not ready: {exc}",
        ) from exc
    return {"status": "ready"}
