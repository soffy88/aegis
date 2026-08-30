"""GitOps Config Sync API — Register repos, trigger syncs, view status."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext, get_current_user
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.gitops import ConfigSyncController, SyncMode, SyncStatus

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/gitops", tags=["gitops"])


class RepoRegister(BaseModel):
    repo_url: str
    branch: str = "main"
    path_prefix: str = ""
    mode: SyncMode = SyncMode.MANUAL
    target_project: UUID | None = None


class RepoOut(BaseModel):
    id: UUID
    repo_url: str
    branch: str
    path_prefix: str
    mode: SyncMode
    target_project: UUID | None
    status: SyncStatus
    last_sync_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@router.post("/repos", response_model=RepoOut, status_code=status.HTTP_201_CREATED)
async def register_repo(
    req: RepoRegister,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIG_APPLY)),
) -> RepoOut:
    """注册 Git 仓库用于配置同步"""
    controller = ConfigSyncController(conn, user.orgs[0].org_id if user.orgs else UUID(int=0))
    sync_id = await controller.register_repo(
        repo_url=req.repo_url,
        branch=req.branch,
        path_prefix=req.path_prefix,
        mode=req.mode,
        target_project=req.target_project,
    )
    row = await conn.fetchrow("SELECT * FROM config_sync_repos WHERE id = $1", sync_id)
    return _row_to_out(row)


@router.get("/repos", response_model=list[RepoOut])
async def list_repos(
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> list[RepoOut]:
    """列出已注册的同步仓库"""
    if not user.orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No org context")
    org_id = user.orgs[0].org_id
    rows = await conn.fetch("SELECT * FROM config_sync_repos WHERE org_id = $1 ORDER BY created_at DESC", org_id)
    return [_row_to_out(r) for r in rows]


@router.post("/repos/{sync_id}/sync", response_model=dict)
async def trigger_sync(
    sync_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIG_APPLY)),
) -> dict:
    """手动触发一次同步"""
    if not user.orgs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No org context")
    controller = ConfigSyncController(conn, user.orgs[0].org_id)
    result = await controller.sync_once(sync_id, actor_user_id=user.user_id)
    return {"status": result.status.value, "drift_detected": result.drift_detected, "error": result.error}


@router.get("/repos/{sync_id}", response_model=RepoOut)
async def get_repo(
    sync_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(get_current_user),
) -> RepoOut:
    row = await conn.fetchrow("SELECT * FROM config_sync_repos WHERE id = $1", sync_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repo not found")
    return _row_to_out(row)


@router.delete("/repos/{sync_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repo(
    sync_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CONFIG_APPLY)),
) -> None:
    await conn.execute("DELETE FROM config_sync_repos WHERE id = $1", sync_id)


def _row_to_out(row: asyncpg.Record) -> RepoOut:
    return RepoOut(
        id=row["id"],
        repo_url=row["repo_url"],
        branch=row["branch"],
        path_prefix=row["path_prefix"],
        mode=SyncMode(row["mode"]),
        target_project=row["target_project"],
        status=SyncStatus(row["status"]),
        last_sync_at=row["last_sync_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# Need imports
from pydantic import BaseModel
from datetime import datetime