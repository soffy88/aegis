"""Installed apps management API."""
from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from aegis.server.api.deps import get_db_conn, require_org, require_project
from aegis.server.persistence import get_pool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/apps", tags=["apps"])


class InstallRequest(BaseModel):
    app_name: str
    app_version: str | None = None
    install_dir: str | None = None
    image_to_pull: str | None = None
    health_check_container: str | None = None
    domain: str | None = None
    domain_target_url: str | None = None
    register_domain: bool = False


async def _run_install(
    install_id: uuid.UUID,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    req: InstallRequest,
    install_dir: str,
) -> None:
    """Background task: run omodul.install_app and update DB status."""
    from omodul.install_app import InstallAppConfig, InstallAppInput, install_app  # noqa: PLC0415

    config = InstallAppConfig(
        register_domain=req.register_domain,
    )
    input_data = InstallAppInput(
        app_name=req.app_name,
        project_dir=install_dir,
        image_to_pull=req.image_to_pull,
        health_check_container=req.health_check_container,
        domain=req.domain,
        domain_target_url=req.domain_target_url,
    )
    output_dir = Path(install_dir) / "aegis_output"

    result = await asyncio.to_thread(install_app, config, input_data, output_dir)

    new_status = result["status"]
    domain = req.domain if result.get("findings", {}).get("domain_registered") else None

    try:
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE installed_apps
                   SET status = $1, domain = $2
                 WHERE id = $3
                """,
                new_status,
                domain,
                install_id,
            )
    except Exception:  # noqa: BLE001
        log.exception("failed to update installed_apps status for %s", install_id)


@router.get("")
async def list_apps(
    conn: asyncpg.Connection = Depends(get_db_conn),
    org_id: uuid.UUID = Depends(require_org),
    project_id: uuid.UUID = Depends(require_project),
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT id, app_name, app_version, install_dir, domain, status, installed_at
          FROM installed_apps
         WHERE org_id = $1 AND project_id = $2
         ORDER BY installed_at DESC
        """,
        org_id,
        project_id,
    )
    return [dict(r) for r in rows]


@router.get("/{install_id}")
async def get_app(
    install_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    org_id: uuid.UUID = Depends(require_org),
) -> dict[str, Any]:
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
    req: InstallRequest,
    background_tasks: BackgroundTasks,
    conn: asyncpg.Connection = Depends(get_db_conn),
    org_id: uuid.UUID = Depends(require_org),
    project_id: uuid.UUID = Depends(require_project),
) -> dict[str, Any]:
    install_dir = req.install_dir or tempfile.mkdtemp(prefix=f"aegis_{req.app_name}_")

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

    background_tasks.add_task(
        _run_install, install_id, org_id, project_id, req, install_dir
    )

    return {"install_id": str(install_id), "status": "installing"}


@router.delete("/{install_id}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_app(
    install_id: uuid.UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    org_id: uuid.UUID = Depends(require_org),
) -> None:
    result = await conn.execute(
        "DELETE FROM installed_apps WHERE id = $1 AND org_id = $2",
        install_id,
        org_id,
    )
    if result == "DELETE 0":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
