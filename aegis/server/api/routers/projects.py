"""Projects API — list registered projects and their health status."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, status

from aegis.server.schemas.project_health import ProjectHealth
from aegis.server.services.project_discovery import discover_projects

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

# In-memory project registry (manual registration, supplemented by discovery)
_PROJECTS: dict[str, dict[str, Any]] = {}

# Health cache: {project_name: (ProjectHealth, fetched_at)}
_HEALTH_CACHE: dict[str, tuple[ProjectHealth, float]] = {}
_CACHE_TTL = 30.0  # seconds


def register_project(name: str, health_url: str) -> None:
    """Register a project for health monitoring (manual or from discovery)."""
    _PROJECTS[name] = {"name": name, "health_url": health_url}


def _get_all_projects() -> dict[str, dict[str, Any]]:
    """Merge manually registered projects with auto-discovered ones."""
    merged = dict(_PROJECTS)
    for proj in discover_projects():
        if proj.name not in merged and proj.health_url:
            merged[proj.name] = {"name": proj.name, "health_url": proj.health_url}
    return merged


async def _fetch_health(name: str, health_url: str) -> ProjectHealth:
    """Fetch health from a project's endpoint with caching."""
    now = time.monotonic()
    cached = _HEALTH_CACHE.get(name)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(health_url)

        if resp.status_code >= 500:
            health = ProjectHealth(status="down", timestamp=datetime.now(tz=UTC))
        elif resp.status_code == 200:
            try:
                data = resp.json()
                health = ProjectHealth(**data)
            except Exception:
                # Old format: HTTP 200 = ok
                health = ProjectHealth(status="ok", timestamp=datetime.now(tz=UTC))
        else:
            health = ProjectHealth(status="down", timestamp=datetime.now(tz=UTC))
    except (httpx.ConnectError, httpx.TimeoutException):
        health = ProjectHealth(status="down", timestamp=datetime.now(tz=UTC))

    _HEALTH_CACHE[name] = (health, now)
    return health


@router.get("")
async def list_projects() -> list[dict[str, Any]]:
    """List all registered + discovered projects with current health status."""
    all_projects = _get_all_projects()
    results = []
    for name, info in all_projects.items():
        health = await _fetch_health(name, info["health_url"])
        results.append(
            {
                "name": name,
                "health_url": info["health_url"],
                "status": health.status,
                "version": health.version,
                "timestamp": health.timestamp.isoformat(),
            }
        )
    return results


@router.get("/{name}/health")
async def get_project_health(name: str) -> ProjectHealth:
    """Get real-time health for a specific project."""
    all_projects = _get_all_projects()
    info = all_projects.get(name)
    if not info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{name}' not registered",
        )
    return await _fetch_health(name, info["health_url"])
