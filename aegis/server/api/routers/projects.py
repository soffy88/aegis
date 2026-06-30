"""Projects CRUD API — org-scoped project management."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from oprim import url_safety_check
from oprim.url_safety_check import URLSafetyError
from pydantic import BaseModel, Field

from aegis.server.api.deps import get_db_conn
from aegis.server.auth.dependencies import UserContext
from aegis.server.auth.rbac import Permission, require_permission
from aegis.server.persistence.audit import record_audit
from aegis.server.repositories.project_repo import ProjectRepository

try:
    from oprim import http_health_probe
except ImportError:  # pragma: no cover
    http_health_probe = None  # type: ignore[assignment]

router = APIRouter(prefix="/api/v1/orgs/{org_id}/projects", tags=["projects"])


def _validate_health_url(url: str) -> None:
    """Service-layer wrapper: calls oprim.url_safety_check + converts to HTTPException.

    Note: DNS-rebinding residual risk documented in oprim.url_safety_check docstring.
    """
    try:
        result = url_safety_check(url=url)
    except URLSafetyError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL validation error: {e}",
        ) from e

    if not result.is_safe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL safety check failed: {result.reason} (failed_check={result.failed_check})",
        )


class ProjectCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    environment: str = "prod"
    docker_labels: dict[str, Any] | None = None
    config: dict[str, Any] | None = None


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    display_name: str | None = Field(None, min_length=1, max_length=100)
    docker_labels: dict[str, Any] | None = None
    config: dict[str, Any] | None = None


def _project_to_dict(project: Any) -> dict[str, Any]:
    return {
        "id": str(project.id),
        "org_id": str(project.org_id),
        "slug": project.slug,
        "name": project.name,
        "display_name": project.display_name,
        "environment": project.environment,
        "docker_labels": project.docker_labels,
        "config": project.config,
        "archived_at": project.archived_at.isoformat() if project.archived_at else None,
        "created_at": project.created_at.isoformat(),
    }


@router.get("")
async def list_projects(
    org_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> list[dict[str, Any]]:
    """List projects in this org. viewer+ can read."""
    project_repo = ProjectRepository(conn)
    projects = await project_repo.list_by_org(org_id, include_archived=False)
    return [_project_to_dict(p) for p in projects]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(
    org_id: UUID,
    req: ProjectCreateRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.CREATE_PROJECT)),
) -> dict[str, Any]:
    """Create a project. member+ required."""
    project_repo = ProjectRepository(conn)
    existing = await project_repo.get_by_org_and_slug(org_id, req.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"slug '{req.slug}' already exists in this org",
        )

    project = await project_repo.create(
        org_id=org_id,
        slug=req.slug,
        name=req.name,
        display_name=req.display_name,
        environment=req.environment,
        docker_labels=req.docker_labels,
        config=req.config,
    )
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="project.created",
        target_type="project",
        target_id=str(project.id),
        metadata={"slug": project.slug, "environment": req.environment},
    )
    return _project_to_dict(project)


@router.get("/{project_id}")
async def get_project(
    org_id: UUID,
    project_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Get a project. viewer+ can read."""
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found in this org"
        )
    return _project_to_dict(project)


@router.patch("/{project_id}")
async def update_project(
    org_id: UUID,
    project_id: UUID,
    req: ProjectUpdateRequest,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.MODIFY_PROJECT)),
) -> dict[str, Any]:
    """Update a project. member+ required."""
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found in this org"
        )

    # Build SET clauses for non-None fields
    updates: list[str] = []
    params: list[Any] = []
    idx = 1

    if req.name is not None:
        updates.append(f"name = ${idx}")
        params.append(req.name)
        idx += 1
    if req.display_name is not None:
        updates.append(f"display_name = ${idx}")
        params.append(req.display_name)
        idx += 1
    if req.docker_labels is not None:
        updates.append(f"docker_labels = ${idx}::jsonb")
        params.append(json.dumps(req.docker_labels))
        idx += 1
    if req.config is not None:
        updates.append(f"config = ${idx}::jsonb")
        params.append(json.dumps(req.config))
        idx += 1

    if not updates:
        return _project_to_dict(project)

    params.append(project_id)
    row = await conn.fetchrow(
        f"UPDATE projects SET {', '.join(updates)} WHERE id = ${idx} RETURNING *",  # noqa: S608
        *params,
    )
    from aegis.server.models import Project  # noqa: PLC0415

    return _project_to_dict(Project.from_row(row))


@router.get("/{project_id}/health")
async def get_project_health(
    org_id: UUID,
    project_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.VIEW_PROJECT)),
) -> dict[str, Any]:
    """Run an HTTP health probe against the project's configured health_url.

    viewer+ required. Returns probe result including healthy, status_code,
    elapsed_ms, and error. Requires project.config['health_url'] to be set.
    """
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="project not found in this org",
        )

    config = project.config or {}
    health_url = config.get("health_url")
    if not health_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project has no health_url configured (set config.health_url)",
        )

    _validate_health_url(health_url)
    # follow_redirects=False: oprim default is already False, but made explicit
    # to prevent a redirect to an internal host bypassing _validate_health_url.
    result = http_health_probe(url=health_url, timeout_sec=5, follow_redirects=False)
    return {
        "project_id": str(project_id),
        "slug": project.slug,
        "health_url": health_url,
        "healthy": result.healthy,
        "status_code": result.status_code,
        "elapsed_ms": result.elapsed_ms,
        "error": result.error,
    }


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_project(
    org_id: UUID,
    project_id: UUID,
    conn: asyncpg.Connection = Depends(get_db_conn),
    user: UserContext = Depends(require_permission(Permission.DELETE_PROJECT)),
) -> None:
    """Archive (soft-delete) a project. admin+ required."""
    project_repo = ProjectRepository(conn)
    project = await project_repo.get_by_id(project_id)
    if not project or project.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found in this org"
        )
    ok = await project_repo.archive(project_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="project already archived")
    await record_audit(
        conn,
        org_id=org_id,
        actor_user_id=user.user_id,
        action="project.archived",
        target_type="project",
        target_id=str(project_id),
        metadata={"slug": project.slug},
    )
