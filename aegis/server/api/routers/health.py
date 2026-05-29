"""Health check + readiness endpoints (公开, 不带 org)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("")
async def health() -> dict[str, Any]:
    """Liveness check — does not touch DB. Public, no auth."""
    return {"status": "ok"}
