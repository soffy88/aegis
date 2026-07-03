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
    s3 = remote_backup.test_connection(cfg)
    dav = remote_backup.test_webdav(cfg)
    return {
        "s3": {
            "configured": remote_backup.is_configured(cfg),
            "bucket": cfg.backup_s3_bucket or None,
            "endpoint": cfg.backup_s3_endpoint_url or None,
            "reachable": s3["ok"],
            "detail": s3["detail"],
        },
        "webdav": {
            "configured": remote_backup.webdav_configured(cfg),
            "url": cfg.backup_webdav_url or None,
            "reachable": dav["ok"],
            "detail": dav["detail"],
        },
    }
