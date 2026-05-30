"""Release gate repository."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import asyncpg

from aegis.server.schemas.release_gate import ReleaseGateResponse


class ReleaseGateRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        requested_by: uuid.UUID,
        action_kind: str,
        action_payload: dict[str, Any],
        autoheal_event_id: uuid.UUID | None,
        expires_in_hours: int,
        now: datetime | None = None,
    ) -> ReleaseGateResponse:
        now = now or datetime.now(UTC)
        expires_at = now + timedelta(hours=expires_in_hours)
        row = await self.conn.fetchrow(
            """
            INSERT INTO release_gates (
                org_id, project_id, autoheal_event_id, action_kind, action_payload,
                requested_by, requested_at, state, expires_at
            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'pending', $8)
            RETURNING *
            """,
            org_id,
            project_id,
            autoheal_event_id,
            action_kind,
            json.dumps(action_payload),
            requested_by,
            now,
            expires_at,
        )
        return ReleaseGateResponse.model_validate(dict(row))

    async def get(
        self,
        *,
        gate_id: uuid.UUID,
        org_id: uuid.UUID,
        lazy_expire: bool = True,
        now: datetime | None = None,
    ) -> ReleaseGateResponse | None:
        """Fetch gate, optionally marking expired-but-pending gates first."""
        now = now or datetime.now(UTC)
        if lazy_expire:
            await self.conn.execute(
                "UPDATE release_gates"
                " SET state = 'expired'"
                " WHERE gate_id = $1 AND org_id = $2 AND state = 'pending' AND expires_at <= $3",
                gate_id,
                org_id,
                now,
            )
        row = await self.conn.fetchrow(
            "SELECT * FROM release_gates WHERE gate_id = $1 AND org_id = $2",
            gate_id,
            org_id,
        )
        return ReleaseGateResponse.model_validate(dict(row)) if row else None

    async def list_by_project(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        state: Literal["pending", "approved", "rejected", "expired"] | None = None,
        limit: int = 100,
        lazy_expire: bool = True,
        now: datetime | None = None,
    ) -> list[ReleaseGateResponse]:
        now = now or datetime.now(UTC)
        if lazy_expire:
            await self.conn.execute(
                "UPDATE release_gates"
                " SET state = 'expired'"
                " WHERE org_id = $1 AND project_id = $2 AND state = 'pending' AND expires_at <= $3",
                org_id,
                project_id,
                now,
            )
        params: list[object] = [org_id, project_id]
        query = "SELECT * FROM release_gates WHERE org_id = $1 AND project_id = $2"
        if state:
            params.append(state)
            query += f" AND state = ${len(params)}"
        params.append(limit)
        query += f" ORDER BY requested_at DESC LIMIT ${len(params)}"
        rows = await self.conn.fetch(query, *params)
        return [ReleaseGateResponse.model_validate(dict(r)) for r in rows]

    async def decide(
        self,
        *,
        gate_id: uuid.UUID,
        org_id: uuid.UUID,
        decided_by: uuid.UUID,
        decision: Literal["approved", "rejected"],
        decision_reason: str,
        now: datetime | None = None,
    ) -> ReleaseGateResponse | None:
        """Atomically approve or reject a pending, non-expired gate.

        Returns None if the gate is already decided, expired, or not found.
        """
        now = now or datetime.now(UTC)
        row = await self.conn.fetchrow(
            """
            UPDATE release_gates
            SET state = $3, decided_by = $4, decided_at = $5, decision_reason = $6
            WHERE gate_id = $1 AND org_id = $2 AND state = 'pending' AND expires_at > $5
            RETURNING *
            """,
            gate_id,
            org_id,
            decision,
            decided_by,
            now,
            decision_reason,
        )
        return ReleaseGateResponse.model_validate(dict(row)) if row else None
