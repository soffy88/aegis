"""ErrorEventRepository — CRUD for error_events hypertable."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from asyncpg import Connection

from aegis.server.schemas.error_monitoring import ErrorEventCreate, ErrorEventResponse


class ErrorEventRepository:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    async def insert(self, *, data: ErrorEventCreate) -> ErrorEventResponse:
        """Single event insert. issue_id left NULL (C3-4 aggregator fills later)."""
        row = await self.conn.fetchrow(
            """
            INSERT INTO error_events (
                org_id, project_id, fingerprint, ts,
                exception_type, exception_value, level, environment,
                server_name, release_name,
                stacktrace, breadcrumbs, user_context, tags, extra,
                sdk_name, sdk_version, platform
            ) VALUES (
                $1, $2, $3, COALESCE($4, NOW()),
                $5, $6, $7, $8,
                $9, $10,
                $11::jsonb, $12::jsonb, $13::jsonb, $14::jsonb, $15::jsonb,
                $16, $17, $18
            )
            RETURNING *
            """,
            data.org_id,
            data.project_id,
            data.fingerprint,
            data.ts,
            data.exception_type,
            data.exception_value,
            data.level,
            data.environment,
            data.server_name,
            data.release_name,
            json.dumps(data.stacktrace) if data.stacktrace is not None else None,
            json.dumps(data.breadcrumbs) if data.breadcrumbs is not None else None,
            json.dumps(data.user_context) if data.user_context is not None else None,
            json.dumps(data.tags) if data.tags is not None else None,
            json.dumps(data.extra) if data.extra is not None else None,
            data.sdk_name,
            data.sdk_version,
            data.platform,
        )
        return self._row_to_response(row)

    async def set_issue_id(
        self,
        *,
        event_id: uuid.UUID,
        issue_id: uuid.UUID,
    ) -> bool:
        """C3-4 aggregator: associate event with its issue after fingerprinting."""
        result = await self.conn.execute(
            "UPDATE error_events SET issue_id = $1 WHERE event_id = $2",
            issue_id,
            event_id,
        )
        return result == "UPDATE 1"

    async def update_fingerprint_and_issue(
        self,
        *,
        event_id: uuid.UUID,
        fingerprint: str,
        issue_id: uuid.UUID,
    ) -> bool:
        """C3-4 aggregator: backfill real fingerprint + issue_id in a single UPDATE."""
        result = await self.conn.execute(
            "UPDATE error_events SET fingerprint = $1, issue_id = $2 WHERE event_id = $3",
            fingerprint,
            issue_id,
            event_id,
        )
        return result == "UPDATE 1"

    async def list_by_issue(
        self,
        *,
        issue_id: uuid.UUID,
        limit: int = 50,
    ) -> list[ErrorEventResponse]:
        rows = await self.conn.fetch(
            "SELECT * FROM error_events WHERE issue_id = $1 ORDER BY ts DESC LIMIT $2",
            issue_id,
            limit,
        )
        return [self._row_to_response(r) for r in rows]

    async def list_by_project(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[ErrorEventResponse]:
        query = "SELECT * FROM error_events WHERE org_id = $1 AND project_id = $2"
        params: list[Any] = [org_id, project_id]
        if since is not None:
            query += f" AND ts >= ${len(params) + 1}"
            params.append(since)
        query += f" ORDER BY ts DESC LIMIT ${len(params) + 1}"
        params.append(limit)
        rows = await self.conn.fetch(query, *params)
        return [self._row_to_response(r) for r in rows]

    def _row_to_response(self, row: Any) -> ErrorEventResponse:
        d = dict(row)
        for field in ("stacktrace", "breadcrumbs", "user_context", "tags", "extra"):
            if d.get(field) and isinstance(d[field], str):
                d[field] = json.loads(d[field])
        return ErrorEventResponse.model_validate(d)
