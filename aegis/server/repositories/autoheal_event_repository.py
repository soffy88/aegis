"""AutoHeal alert-event repository (audit P0 #3).

`aegis_alert_events` was an orphan table — created in migrations but written by
nothing, so the autoheal dashboard/stats were permanently empty. This repo gives
it a real writer (alert fires) plus the read/handle paths the router needs.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg


class AutoHealEventRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def insert(
        self,
        *,
        org_id: uuid.UUID,
        cycle_id: uuid.UUID,
        severity: str,
        source: str,
        reason: str,
        value: float | None = None,
    ) -> uuid.UUID:
        """Record an autoheal-relevant alert event. Returns the new row id."""
        row = await self.conn.fetchrow(
            """
            INSERT INTO aegis_alert_events
                (cycle_id, severity, source, reason, value, org_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            cycle_id,
            severity,
            source,
            reason,
            value,
            org_id,
        )
        return row["id"]

    async def list_for_org(
        self, *, org_id: uuid.UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = await self.conn.fetch(
            "SELECT * FROM aegis_alert_events WHERE org_id = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            org_id,
            limit,
        )
        return [dict(r) for r in rows]

    async def get(
        self, *, org_id: uuid.UUID, event_id: uuid.UUID
    ) -> dict[str, Any] | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM aegis_alert_events WHERE org_id = $1 AND id = $2",
            org_id,
            event_id,
        )
        return dict(row) if row else None

    async def mark_handled(self, *, org_id: uuid.UUID, event_id: uuid.UUID) -> bool:
        """Mark an event handled. Returns False if it didn't exist."""
        result = await self.conn.execute(
            "UPDATE aegis_alert_events SET handled = true, handled_at = now() "
            "WHERE org_id = $1 AND id = $2",
            org_id,
            event_id,
        )
        return result != "UPDATE 0"

    async def stats(self, *, org_id: uuid.UUID) -> dict[str, Any]:
        row = await self.conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE created_at > now() - interval '24h') AS today_total,
                COUNT(*) FILTER (
                    WHERE handled = true AND created_at > now() - interval '24h'
                ) AS today_handled,
                COUNT(*) FILTER (WHERE handled = false AND severity = 'critical')
                    AS pending_critical,
                COUNT(*) FILTER (WHERE handled = false) AS pending_total
            FROM aegis_alert_events
            WHERE org_id = $1
            """,
            org_id,
        )
        if not row:
            return {
                "today_total": 0,
                "today_handled": 0,
                "pending_critical": 0,
                "pending_total": 0,
            }
        return dict(row)
