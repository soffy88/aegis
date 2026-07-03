"""Backup storage status — reports whether S3-compatible remote backup is
configured and reachable (used by the console's backup settings)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends

from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.runtime.config import get_settings

router = APIRouter(prefix="/api/v1/orgs/{org_id}/backup-storage", tags=["backup-storage"])


@router.get("")
async def status(
    org_id: uuid.UUID,
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    from aegis.server.services import remote_backup  # noqa: PLC0415

    cfg = get_settings()
    conn = remote_backup.test_connection(cfg)
    return {
        "configured": remote_backup.is_configured(cfg),
        "bucket": cfg.backup_s3_bucket or None,
        "endpoint": cfg.backup_s3_endpoint_url or None,
        "reachable": conn["ok"],
        "detail": conn["detail"],
    }
