"""Alert rule CRUD repository."""

from __future__ import annotations

import uuid

import asyncpg

from aegis.server.schemas.alerting import AlertRuleCreate, AlertRuleResponse, AlertRuleUpdate


class AlertRuleRepository:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        created_by: uuid.UUID,
        data: AlertRuleCreate,
    ) -> AlertRuleResponse:
        row = await self.conn.fetchrow(
            """
            INSERT INTO alert_rules (
                org_id, project_id, name, metric,
                threshold_warn, threshold_critical, operator,
                throttle_seconds, escalation_delay_seconds, dedup_bucket_seconds,
                enabled, created_by
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            RETURNING *
            """,
            org_id,
            project_id,
            data.name,
            data.metric,
            data.threshold_warn,
            data.threshold_critical,
            data.operator,
            data.throttle_seconds,
            data.escalation_delay_seconds,
            data.dedup_bucket_seconds,
            data.enabled,
            created_by,
        )
        return AlertRuleResponse.model_validate(dict(row))

    async def get(
        self,
        *,
        rule_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> AlertRuleResponse | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM alert_rules WHERE rule_id=$1 AND org_id=$2",
            rule_id,
            org_id,
        )
        return AlertRuleResponse.model_validate(dict(row)) if row else None

    async def list_by_project(
        self,
        *,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        enabled_only: bool = False,
    ) -> list[AlertRuleResponse]:
        query = "SELECT * FROM alert_rules WHERE org_id=$1 AND project_id=$2"
        if enabled_only:
            query += " AND enabled = TRUE"
        query += " ORDER BY created_at DESC"
        rows = await self.conn.fetch(query, org_id, project_id)
        return [AlertRuleResponse.model_validate(dict(r)) for r in rows]

    async def update(
        self,
        *,
        rule_id: uuid.UUID,
        org_id: uuid.UUID,
        data: AlertRuleUpdate,
    ) -> AlertRuleResponse | None:
        updates = data.model_dump(exclude_unset=True, exclude_none=True)
        if not updates:
            return await self.get(rule_id=rule_id, org_id=org_id)
        set_clauses = ", ".join(f"{k} = ${i + 3}" for i, k in enumerate(updates.keys()))
        query = (
            f"UPDATE alert_rules SET {set_clauses}, updated_at = NOW()"
            " WHERE rule_id=$1 AND org_id=$2 RETURNING *"
        )
        row = await self.conn.fetchrow(query, rule_id, org_id, *updates.values())
        return AlertRuleResponse.model_validate(dict(row)) if row else None

    async def delete(
        self,
        *,
        rule_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> bool:
        result = await self.conn.execute(
            "DELETE FROM alert_rules WHERE rule_id=$1 AND org_id=$2",
            rule_id,
            org_id,
        )
        return result == "DELETE 1"
