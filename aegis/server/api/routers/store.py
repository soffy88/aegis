"""App Store API — browse and search available apps."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/orgs/{org_id}/store", tags=["store"])

_apps_cache: list[dict[str, Any]] | None = None


def _load_apps() -> list[dict[str, Any]]:
    global _apps_cache  # noqa: PLW0603
    if _apps_cache is not None:
        return _apps_cache

    settings = AegisSettings()
    store_dir = Path(settings.data_dir).parent / "aegis-appstore" / "apps"
    if not store_dir.exists():
        # Fallback: sibling repo
        store_dir = Path(__file__).resolve().parents[4] / "aegis-appstore" / "apps"

    apps: list[dict[str, Any]] = []
    if store_dir.exists():
        for f in sorted(store_dir.glob("*.json")):
            try:
                apps.append(json.loads(f.read_text()))
            except Exception:
                log.warning("Failed to parse app definition: %s", f)
    _apps_cache = apps
    return apps


@router.get("")
async def list_catalog(
    org_id: UUID,
    q: str | None = Query(default=None, description="Search query"),
    category: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=30, ge=1, le=100),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """List available apps in the store catalog. viewer+ can browse."""
    apps = _load_apps()

    if q:
        q_lower = q.lower()
        apps = [
            a
            for a in apps
            if q_lower in a.get("name", "").lower() or q_lower in a.get("description", "").lower()
        ]
    if category:
        apps = [a for a in apps if a.get("category", "").lower() == category.lower()]

    total = len(apps)
    start = (page - 1) * per_page
    items = apps[start : start + per_page]

    return {"total": total, "page": page, "per_page": per_page, "items": items}


@router.get("/{app_slug}")
async def get_catalog_item(
    org_id: UUID,
    app_slug: str,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get a single app definition by slug. viewer+ can view."""
    apps = _load_apps()
    for app in apps:
        if app.get("slug") == app_slug:
            return app
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"App '{app_slug}' not found")
