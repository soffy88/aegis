"""ErrorIssueRepository — CRUD for error_issues aggregation table."""

from __future__ import annotations

import uuid
from typing import Any

from asyncpg import Connection

from aegis.server.schemas.error_monitoring import ErrorIssueResponse


class ErrorIssueRepository:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    async def upsert_by_fingerprint(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        fingerprint: str,
        exception_type: str,
        exception_value: str | None,
        release_name: str | None = None,
    ) -> tuple[ErrorIssueResponse, bool]:
        """UPSERT by UNIQUE(org_id, project_id, fingerprint).

        Existing → event_count++ + last_seen + last_release update → returns (issue, False).
        New → insert → returns (issue, True).
        (xmax = 0) AS is_new distinguishes INSERT vs UPDATE (same pattern as C2-2).
        """
        row = await self.conn.fetchrow(
            """
            INSERT INTO error_issues (
                org_id, project_id, fingerprint,
                exception_type, exception_value,
                event_count, last_seen, first_release, last_release
            ) VALUES ($1, $2, $3, $4, $5, 1, NOW(), $6, $6)
            ON CONFLICT (org_id, project_id, fingerprint) DO UPDATE SET
                event_count = error_issues.event_count + 1,
                last_seen   = NOW(),
                last_release = EXCLUDED.last_release,
                updated_at  = NOW()
            RETURNING *, (xmax = 0) AS is_new
            """,
            org_id,
            project_id,
            fingerprint,
            exception_type,
            exception_value,
            release_name,
        )
        d = dict(row)
        is_new: bool = d.pop("is_new")
        return ErrorIssueResponse.model_validate(d), is_new

    async def get(
        self,
        *,
        issue_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> ErrorIssueResponse | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM error_issues WHERE issue_id = $1 AND org_id = $2",
            issue_id,
            org_id,
        )
        return ErrorIssueResponse.model_validate(dict(row)) if row else None

    async def list_by_project(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        state: str | None = None,
        limit: int = 100,
    ) -> list[ErrorIssueResponse]:
        query = "SELECT * FROM error_issues WHERE org_id = $1 AND project_id = $2"
        params: list[Any] = [org_id, project_id]
        if state is not None:
            query += f" AND state = ${len(params) + 1}"
            params.append(state)
        query += f" ORDER BY last_seen DESC LIMIT ${len(params) + 1}"
        params.append(limit)
        rows = await self.conn.fetch(query, *params)
        return [ErrorIssueResponse.model_validate(dict(r)) for r in rows]

    async def mark_resolved(
        self,
        *,
        issue_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> bool:
        """M3+ reserved — schema + method exist but no router exposes this in M1."""
        result = await self.conn.execute(
            """
            UPDATE error_issues
            SET state = 'resolved', updated_at = NOW()
            WHERE issue_id = $1 AND org_id = $2
            """,
            issue_id,
            org_id,
        )
        return result == "UPDATE 1"
