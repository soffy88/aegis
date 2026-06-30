"""Backups management API."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence.db import get_pool
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/backups", tags=["backups"])


class BackupRequest(BaseModel):
    app_slug: str
    instance_name: str
    target_volume: str


class RestoreRequest(BaseModel):
    target_volume: str
    container_id: str | None = None


@router.get("")
async def list_backups(
    org_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List all backups for an organization."""
    rows = await conn.fetch(
        "SELECT * FROM aegis_backups WHERE org_id = $1 ORDER BY created_at DESC", org_id
    )
    return [dict(r) for r in rows]


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_backup(
    org_id: uuid.UUID,
    body: BackupRequest,
    background_tasks: BackgroundTasks,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_PROJECT)),
) -> dict[str, Any]:
    """Trigger a new backup."""
    backup_id = await conn.fetchval(
        """
        INSERT INTO aegis_backups (org_id, app_slug, instance_name, status)
        VALUES ($1, $2, $3, 'pending')
        RETURNING id
        """,
        org_id,
        body.app_slug,
        body.instance_name,
    )
    background_tasks.add_task(_run_backup, backup_id, org_id, body)
    return {"backup_id": str(backup_id), "status": "pending"}


@router.get("/{backup_id}")
async def get_backup(
    org_id: uuid.UUID,
    backup_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get single backup detail."""
    row = await conn.fetchrow(
        "SELECT * FROM aegis_backups WHERE org_id = $1 AND id = $2", org_id, backup_id
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backup not found")
    return dict(row)


@router.post("/{backup_id}/restore", status_code=status.HTTP_202_ACCEPTED)
async def restore_backup(
    org_id: uuid.UUID,
    backup_id: uuid.UUID,
    body: RestoreRequest,
    background_tasks: BackgroundTasks,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_PROJECT)),
) -> dict[str, Any]:
    """Restore from an existing backup."""
    row = await conn.fetchrow(
        "SELECT * FROM aegis_backups WHERE org_id = $1 AND id = $2", org_id, backup_id
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backup not found")

    if row["status"] != "completed":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot restore from incomplete backup")

    await conn.execute("UPDATE aegis_backups SET status = 'restoring' WHERE id = $1", backup_id)

    background_tasks.add_task(_run_restore, backup_id, org_id, body, dict(row))
    return {"message": "restore task submitted"}


@router.delete("/{backup_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backup(
    org_id: uuid.UUID,
    backup_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_PROJECT)),
) -> None:
    """Delete a backup record."""
    result = await conn.execute(
        "DELETE FROM aegis_backups WHERE org_id = $1 AND id = $2", org_id, backup_id
    )
    if result == "DELETE 0":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backup not found")


async def _run_backup(backup_id: uuid.UUID, org_id: uuid.UUID, body: BackupRequest) -> None:
    from omodul.backup_app_data import BackupAppDataConfig, BackupAppDataInput, backup_app_data

    cfg = get_settings()

    if cfg.backup_s3_bucket:
        target = f"s3://{cfg.backup_s3_bucket}/{org_id}/{body.app_slug}/"
    else:
        target = f"file://{cfg.backup_local_dir}/{org_id}/{body.app_slug}/"

    config = BackupAppDataConfig(
        instance_name=body.instance_name,
        backup_target=target,
    )
    input_data = BackupAppDataInput(
        container_id="",
        volumes_to_backup=[body.target_volume],
        config_paths=[],
    )

    try:
        result = await asyncio.to_thread(
            backup_app_data, config, input_data, output_dir=cfg.backup_local_dir / str(org_id)
        )

        # backup_app_data returns {"status", "findings", "error", ...}; the backup
        # key / size live on the nested `findings` object, NOT at the top level
        # (the old code read result.get("storage_url") which was always None →
        # backup_key=NULL). It also reports failures via status without raising.
        findings = result.get("findings")
        if result.get("status") != "completed" or findings is None:
            err = (result.get("error") or {}).get("error_message") or "backup did not complete"
            raise RuntimeError(err)

        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE aegis_backups
                   SET status = 'completed',
                       backup_key = $1,
                       size_bytes = $2,
                       completed_at = $3
                 WHERE id = $4
                """,
                getattr(findings, "storage_url", None),
                getattr(findings, "total_size_bytes", 0) or 0,
                datetime.now(UTC),
                backup_id,
            )
    except Exception as exc:
        log.exception("backup_failed id=%s", backup_id)
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE aegis_backups SET status = 'failed', error = $1 WHERE id = $2",
                str(exc),
                backup_id,
            )


async def _run_restore(
    backup_id: uuid.UUID, org_id: uuid.UUID, body: RestoreRequest, backup_row: dict[str, Any]
) -> None:
    from oskill.restore_from_backup import restore_from_backup

    cfg = get_settings()
    try:
        if not backup_row.get("backup_key"):
            raise RuntimeError(
                "backup has no backup_key (backup never completed an upload) — "
                "cannot restore"
            )
        await asyncio.to_thread(
            restore_from_backup,
            app_slug=backup_row["app_slug"],
            backup_bucket=cfg.backup_s3_bucket,
            backup_key=backup_row["backup_key"] or "",
            target_volume=body.target_volume,
            aws_endpoint_url=cfg.backup_s3_endpoint_url,
            aws_access_key_id=cfg.backup_s3_access_key_id,
            aws_secret_access_key=cfg.backup_s3_secret_access_key,
        )

        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE aegis_backups SET status = 'completed' WHERE id = $1", backup_id
            )
    except Exception as exc:
        log.exception("restore_failed id=%s", backup_id)
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE aegis_backups SET status = 'failed', error = $1 WHERE id = $2",
                str(exc),
                backup_id,
            )
