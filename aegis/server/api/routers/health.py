"""Health check + readiness endpoints."""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from aegis.server.api.deps import get_db_conn
from aegis.server.persistence import get_pool

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness check — does not touch DB."""
    return {"status": "ok", "service": "aegis"}


@router.get("/ready")
async def ready(
    conn: asyncpg.Connection = Depends(get_db_conn),
) -> dict[str, Any]:
    """Readiness check — verifies DB connectivity."""
    try:
        row = await conn.fetchrow("SELECT 1 AS ok")
        if row is None or row["ok"] != 1:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="DB not ready",
            )
    except (asyncpg.PostgresError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"DB error: {exc}",
        ) from exc

    return {"status": "ready", "db": "ok"}


@router.get("/metrics/pool")
async def pool_metrics() -> dict[str, Any]:
    """Postgres pool statistics."""
    pool = get_pool()
    return {
        "size": pool.get_size(),
        "min_size": pool.get_min_size(),
        "max_size": pool.get_max_size(),
        "idle_size": pool.get_idle_size(),
    }
