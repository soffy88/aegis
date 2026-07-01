"""Deploy a container straight from a Git repository (build → run).

POST builds the repo's Dockerfile and starts the image as a managed container,
tracked in installed_apps (status building → completed/failed). member+ required.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence import get_pool
from aegis.server.repositories.project_repo import ProjectRepository

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/orgs/{org_id}/git-deploy", tags=["git-deploy"])


class GitDeployRequest(BaseModel):
    repo_url: str = Field(min_length=1, max_length=2000)
    app_name: str = Field(min_length=1, max_length=63)
    branch: str | None = None
    subdir: str | None = None
    ports: list[int] = Field(default_factory=list)
    env: list[dict[str, Any]] = Field(default_factory=list)


async def _run_git_deploy(
    install_id: uuid.UUID, app_name: str, req: GitDeployRequest, docker_host: str
) -> None:
    import tempfile  # noqa: PLC0415

    from aegis.server.services.git_deploy import build_and_deploy_from_git  # noqa: PLC0415

    # Build context lives in a writable temp dir (the data_dir volume is root-owned;
    # the server runs non-root). Cloned sources are removed after each build.
    build_root = str(Path(tempfile.gettempdir()) / "aegis-git-builds")
    final_status = "failed"
    image: str | None = None
    error: str | None = None
    try:
        image = await build_and_deploy_from_git(
            repo_url=req.repo_url,
            branch=req.branch,
            app_name=app_name,
            subdir=req.subdir,
            ports=req.ports,
            env=req.env,
            docker_host=docker_host,
            build_root=build_root,
        )
        final_status = "completed"
    except Exception as exc:  # noqa: BLE001
        log.exception("git_deploy_failed install_id=%s", install_id)
        error = f"{type(exc).__name__}: {exc}"

    try:
        async with get_pool().acquire() as conn:
            await conn.execute(
                "UPDATE installed_apps SET status = $1, image = COALESCE($2, image) WHERE id = $3",
                final_status,
                image,
                install_id,
            )
    except Exception:  # noqa: BLE001
        log.exception("git_deploy status update failed install_id=%s (error=%s)", install_id, error)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def git_deploy_endpoint(
    org_id: uuid.UUID,
    req: GitDeployRequest,
    background_tasks: BackgroundTasks,
    project_id: uuid.UUID = Query(..., description="Project to deploy into"),
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.INSTALL_APP)),
) -> dict[str, Any]:
    """Build a Git repo's Dockerfile and run it as a managed container. member+ required."""
    project = await ProjectRepository(conn).get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found in this org")

    from aegis.server.runtime.config import get_settings  # noqa: PLC0415

    docker_host = get_settings().docker_host

    install_id = await conn.fetchval(
        "INSERT INTO installed_apps (org_id, project_id, app_name, app_version, install_dir, status)"
        " VALUES ($1,$2,$3,'git','/','building')"
        " ON CONFLICT (org_id, project_id, app_name)"
        "   DO UPDATE SET status='building', installed_at = now()"
        " RETURNING id",
        org_id,
        project_id,
        req.app_name,
    )
    background_tasks.add_task(_run_git_deploy, install_id, req.app_name, req, docker_host)
    return {"install_id": str(install_id), "status": "building"}
