"""Project repository."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg

from aegis.server.models import Project


class ProjectRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(
        self,
        *,
        org_id: UUID,
        slug: str,
        name: str,
        display_name: str,
        environment: str = "prod",
        docker_labels: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> Project:
        import json

        row = await self.conn.fetchrow(
            """INSERT INTO projects
               (org_id, slug, name, display_name, environment, docker_labels, config)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb) RETURNING *""",
            org_id,
            slug,
            name,
            display_name,
            environment,
            json.dumps(docker_labels) if docker_labels else None,
            json.dumps(config) if config else None,
        )
        return Project.from_row(row)

    async def get_by_id(self, project_id: UUID) -> Project | None:
        row = await self.conn.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
        return Project.from_row(row) if row else None

    async def get_by_org_and_slug(self, org_id: UUID, slug: str) -> Project | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM projects WHERE org_id = $1 AND slug = $2",
            org_id,
            slug,
        )
        return Project.from_row(row) if row else None

    async def list_by_org(self, org_id: UUID, *, include_archived: bool = False) -> list[Project]:
        if include_archived:
            rows = await self.conn.fetch(
                "SELECT * FROM projects WHERE org_id = $1 ORDER BY created_at", org_id
            )
        else:
            rows = await self.conn.fetch(
                """SELECT * FROM projects
                   WHERE org_id = $1 AND archived_at IS NULL
                   ORDER BY created_at""",
                org_id,
            )
        return [Project.from_row(r) for r in rows]

    async def update_config(self, project_id: UUID, config: dict[str, Any]) -> Project | None:
        import json

        row = await self.conn.fetchrow(
            "UPDATE projects SET config = $1::jsonb WHERE id = $2 RETURNING *",
            json.dumps(config),
            project_id,
        )
        return Project.from_row(row) if row else None

    async def get_by_id_and_public_key(
        self, *, project_id: UUID, public_key: str
    ) -> Project | None:
        """C3-6 envelope router auth: verify (project_id, public_key) pair.

        Returns Project (contains org_id needed by envelope router), or None on
        auth failure. sentry_public_key is never exposed in public API responses.
        """
        row = await self.conn.fetchrow(
            "SELECT * FROM projects WHERE id = $1 AND sentry_public_key = $2",
            project_id,
            public_key,
        )
        return Project.from_row(row) if row else None

    async def archive(self, project_id: UUID) -> bool:
        result = await self.conn.execute(
            "UPDATE projects SET archived_at = NOW() WHERE id = $1 AND archived_at IS NULL",
            project_id,
        )
        return result == "UPDATE 1"
