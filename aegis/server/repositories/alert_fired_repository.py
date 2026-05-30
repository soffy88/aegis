"""Alert fired history repository."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import asyncpg

from aegis.server.schemas.alerting import AlertFiredResponse


class AlertFiredRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def upsert_or_update_last_seen(
        self,
        *,
        rule_id: uuid.UUID,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        dedup_key: str,
        severity: str,
        current_value: float | None,
        triggered_reason: str | None,
        now: datetime | None = None,
    ) -> tuple[AlertFiredResponse, bool]:
        """同桶 dedup_key 唯一; 已存在 → 更新 last_seen_at; 不存在 → INSERT.

        Returns:
            (AlertFiredResponse, is_new) — is_new=True 表示本次是首次触发.
        """
        now = now or datetime.now(UTC)
        row = await self.conn.fetchrow(
            """
            INSERT INTO alert_fired_history (
                rule_id, org_id, project_id, dedup_key,
                severity, current_value, triggered_reason, last_seen_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (dedup_key) DO UPDATE SET
                last_seen_at = EXCLUDED.last_seen_at
            RETURNING *, (xmax = 0) AS is_new
            """,
            rule_id,
            org_id,
            project_id,
            dedup_key,
            severity,
            current_value,
            triggered_reason,
            now,
        )
        is_new: bool = row["is_new"]
        row_dict = {k: v for k, v in row.items() if k != "is_new"}
        return AlertFiredResponse.model_validate(row_dict), is_new

    async def list_by_project(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        limit: int = 100,
        severity: str | None = None,
    ) -> list[AlertFiredResponse]:
        params: list[object] = [org_id, project_id]
        query = "SELECT * FROM alert_fired_history WHERE org_id=$1 AND project_id=$2"
        if severity:
            params.append(severity)
            query += f" AND severity = ${len(params)}"
        params.append(limit)
        query += f" ORDER BY fired_at DESC LIMIT ${len(params)}"
        rows = await self.conn.fetch(query, *params)
        return [AlertFiredResponse.model_validate(dict(r)) for r in rows]

    async def get_last_fired(
        self,
        *,
        rule_id: uuid.UUID,
    ) -> AlertFiredResponse | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM alert_fired_history WHERE rule_id=$1 ORDER BY fired_at DESC LIMIT 1",
            rule_id,
        )
        return AlertFiredResponse.model_validate(dict(row)) if row else None

    async def mark_escalated(
        self,
        *,
        fired_id: uuid.UUID,
        escalated_at: datetime | None = None,
    ) -> bool:
        escalated_at = escalated_at or datetime.now(UTC)
        result = await self.conn.execute(
            "UPDATE alert_fired_history"
            " SET escalated_at = $1"
            " WHERE fired_id = $2 AND escalated_at IS NULL",
            escalated_at,
            fired_id,
        )
        return result == "UPDATE 1"
