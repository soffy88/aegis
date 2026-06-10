"""Installed apps management API."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence import get_pool
from aegis.server.persistence.event_trail import append_event
from aegis.server.repositories.project_repo import ProjectRepository
from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/apps", tags=["apps"])


class InstallRequest(BaseModel):
    app_name: str
    app_version: str | None = None
    install_dir: str = Field(..., min_length=1)
    image_to_pull: str | None = None
    health_check_container: str | None = None
    domain: str | None = None
    domain_target_url: str | None = None
    register_domain: bool = False

    @field_validator("install_dir")
    @classmethod
    def install_dir_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("install_dir is required")
        return v


class InstallAppRequest(BaseModel):
    """Internal request model for _run_install (project_dir is pre-computed)."""

    app_name: str
    project_dir: str | None = None
    app_version: str | None = None
    image_to_pull: str | None = None
    health_check_container: str | None = None
    domain: str | None = None
    domain_target_url: str | None = None
    register_domain: bool = False


async def _run_install(
    install_id: uuid.UUID,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    trace_id: str,
    body: InstallAppRequest,
    data_dir: Path | None = None,
) -> None:
    """Background task: invoke omodul.install_self_hosted_app via dispatcher."""
    from aegis.server.dispatch.budget_tracker import BudgetTracker  # noqa: PLC0415
    from aegis.server.dispatch.dedup_cache import DedupCache  # noqa: PLC0415
    from aegis.server.dispatch.omodul_dispatcher import OmodulDispatcher  # noqa: PLC0415
    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    cfg = get_settings()
    resolved_dir: Path = data_dir if data_dir is not None else cfg.data_dir

    final_status: str = "failed"
    error_detail: str | None = None
    domain: str | None = None

    try:
        import redis.asyncio as aioredis  # noqa: PLC0415

        redis_client = aioredis.from_url(cfg.redis_url)
        dispatcher = OmodulDispatcher(
            DedupCache(redis_client),
            BudgetTracker(redis_client),
            data_dir=str(resolved_dir),
        )

        result: Any = await dispatcher.invoke(
            omodul_name="install_self_hosted_app",
            config={
                "app_slug": body.app_name,
                "app_version": body.app_version or "latest",
                "instance_name": body.app_name,
                "config_hash": "",
                "domain": body.domain or "",
            },
            input_data={
                "app_config": {
                    "image_to_pull": body.image_to_pull,
                    "project_dir": body.project_dir,
                },
                "target_host": "localhost",
                "docker_host": "unix:///var/run/docker.sock",
                "caddy_admin_url": "http://localhost:2019",
            },
            user_id=str(org_id),
            project_id=project_id,
        )
        final_status = str(result.get("status", "failed"))
        if result.get("error"):
            error_detail = str(result["error"])
        await redis_client.aclose()
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "install dispatch failed install_id=%s %s: %s",
            install_id,
            type(exc).__name__,
            exc,
        )
        final_status = "failed"
        error_detail = f"{type(exc).__name__}: {exc}"

    try:
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE installed_apps
                   SET status = $1, domain = $2
                 WHERE id = $3
                """,
                final_status,
                domain,
                install_id,
            )
            try:
                await append_event(
                    conn=conn,
                    org_id=org_id,
                    project_id=project_id,
                    event_type="omodul_run",
                    severity="info" if final_status == "completed" else "warning",
                    resource=f"app/{body.app_name}",
                    omodul_kind="install_app",
                    payload={
                        "install_id": str(install_id),
                        "final_status": final_status,
                        "error_detail": error_detail,
                    },
                    trace_id=trace_id,
                    initiated_by="agent",
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to append event_trail for install %s", install_id)
    except Exception:  # noqa: BLE001
        log.exception("failed to update installed_apps status for %s", install_id)


@router.get("")
async def list_apps(
    org_id: uuid.UUID,
    project_id: uuid.UUID | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List installed apps. project_id=None returns all in this org."""
    rows = await conn.fetch(
        """
        SELECT id, app_name, app_version, install_dir, domain, status, installed_at
          FROM installed_apps
         WHERE org_id = $1 AND ($2::uuid IS NULL OR project_id = $2)
         ORDER BY installed_at DESC
        """,
        org_id,
        project_id,
    )
    return [dict(r) for r in rows]


@router.get("/{install_id}")
async def get_app(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get an installed app by ID. viewer+ can read."""
    row = await conn.fetchrow(
        "SELECT * FROM installed_apps WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    return dict(row)


@router.post("/install", status_code=status.HTTP_202_ACCEPTED)
async def install_app_endpoint(
    org_id: uuid.UUID,
    req: InstallRequest,
    background_tasks: BackgroundTasks,
    project_id: uuid.UUID = Query(..., description="Project to install the app into"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Install an app into a project. member+ required."""
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found in this org"
        )

    install_dir = req.install_dir
    cfg_data_dir = get_settings().data_dir

    install_id = await conn.fetchval(
        """
        INSERT INTO installed_apps (org_id, project_id, app_name, app_version, install_dir, status)
        VALUES ($1, $2, $3, $4, $5, 'installing')
        ON CONFLICT (org_id, project_id, app_name)
            DO UPDATE SET status = 'installing', installed_at = now()
        RETURNING id
        """,
        org_id,
        project_id,
        req.app_name,
        req.app_version,
        install_dir,
    )

    body = InstallAppRequest(
        app_name=req.app_name,
        project_dir=install_dir,
        app_version=req.app_version,
        image_to_pull=req.image_to_pull,
        health_check_container=req.health_check_container,
        domain=req.domain,
        domain_target_url=req.domain_target_url,
        register_domain=req.register_domain,
    )
    task_trace_id = f"trc_{uuid.uuid4().hex[:8]}"
    background_tasks.add_task(
        _run_install, install_id, org_id, project_id, task_trace_id, body, cfg_data_dir
    )

    return {"install_id": str(install_id), "status": "installing"}


@router.delete("/{install_id}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_app(
    org_id: uuid.UUID,
    install_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> None:
    """Uninstall an app. member+ required."""
    result = await conn.execute(
        "DELETE FROM installed_apps WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
