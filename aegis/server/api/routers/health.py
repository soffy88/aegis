"""Health check + readiness + qualification endpoints (公开, 不带 org)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from aegis.server.orchestration.loop_supervisor import get_loops_health
from aegis.server.persistence import get_pool
from aegis.server.services.safety_mode import SafetyMode, compute_safety_mode

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


@router.get("/qualification")
async def qualification() -> dict[str, Any]:
    """Production qualification — computes unified SafetyMode.

    Returns QUALIFIED | DEGRADED | BLOCKED with failed/degraded checks.
    All mutation endpoints and background actuators MUST use the same gate.
    """
    try:
        async with get_pool().acquire() as conn:
            qual = await compute_safety_mode(conn)
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"qualification failed: {exc}",
        ) from exc

    if qual.mode == SafetyMode.BLOCKED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"safety_mode=BLOCKED: {qual.failed_checks}",
        )

    return {
        "mode": qual.mode.value,
        "failed_checks": qual.failed_checks,
        "degraded_checks": qual.degraded_checks,
        "details": [
            {"name": d.name, "passed": d.passed, "message": d.message} for d in qual.details
        ],
    }


@router.get("/loops")
async def loops_health() -> dict[str, Any]:
    """Loop supervision health — returns status of all supervised loops.

    Includes: status, restart_count, last_error, last_tick, overdue, never_seen.
    External heartbeat is allowed only when no required loop is dead.
    """
    try:
        return await get_loops_health()
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"loops health failed: {exc}",
        ) from exc
