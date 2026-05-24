"""event_trail writer + reader."""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

import asyncpg

EventType = Literal[
    "alert_fired",
    "alert_resolved",
    "alert_acked",
    "deploy",
    "config_change",
    "release_gate_decision",
    "omodul_run",
    "oskill_step",
    "llm_call",
    "autoheal_triggered",
    "autoheal_executed",
    "autoheal_failed",
    "state_change",
    "metric_anomaly",
    "log_anomaly",
    "incident_created",
    "incident_resolved",
    "user_action",
    "system_event",
    "docker_event",
]


async def append_event(
    *,
    conn: asyncpg.Connection,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
    severity: str = "info",
    user_id: uuid.UUID | None = None,
    service: str | None = None,
    resource: str | None = None,
    environment: str = "prod",
    trace_id: str | None = None,
    parent_id: uuid.UUID | None = None,
    root_cause_id: uuid.UUID | None = None,
    omodul_fingerprint: str | None = None,
    omodul_kind: str | None = None,
    autoheal_plugin: str | None = None,
    autoheal_result: dict[str, Any] | None = None,
    initiated_by: str = "system",
    approved_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """Append an event to event_trail. Returns the new event id."""
    row = await conn.fetchrow(
        """
        INSERT INTO event_trail (
            org_id, project_id, user_id,
            service, resource, environment,
            event_type, severity, payload,
            trace_id, parent_id, root_cause_id,
            omodul_fingerprint, omodul_kind,
            autoheal_plugin, autoheal_result,
            initiated_by, approved_by
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb,
            $10, $11, $12, $13, $14, $15, $16::jsonb, $17, $18
        ) RETURNING id
        """,
        org_id,
        project_id,
        user_id,
        service,
        resource,
        environment,
        event_type,
        severity,
        json.dumps(payload or {}),
        trace_id,
        parent_id,
        root_cause_id,
        omodul_fingerprint,
        omodul_kind,
        autoheal_plugin,
        json.dumps(autoheal_result) if autoheal_result else None,
        initiated_by,
        approved_by,
    )
    return uuid.UUID(str(row["id"]))


async def save_decision_trail(
    *,
    omodul_name: str,
    fingerprint: str,
    decision_trail: dict[str, Any],
    user_id: str,
    status: str,
    error: dict[str, Any] | None = None,
    report_path: str | None = None,
) -> None:
    """Persist omodul decision_trail to Postgres (additive, not replacing omodul's JSON).

    Idempotent: ON CONFLICT (omodul_fingerprint) DO NOTHING (ADR-002 M1 方案 A).
    """
    from aegis.server.persistence.db import get_pool  # noqa: PLC0415

    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO event_trail (
                org_id, project_id, event_type, severity, payload,
                omodul_fingerprint, omodul_kind, initiated_by
            ) VALUES (
                '00000000-0000-0000-0000-000000000001'::uuid,
                '00000000-0000-0000-0000-000000000002'::uuid,
                'omodul_run', $1, $2::jsonb,
                $3, $4, 'dispatcher'
            )
            ON CONFLICT (omodul_fingerprint) DO NOTHING
            """,
            "info" if status == "completed" else "warning",
            json.dumps({
                "decision_trail": decision_trail,
                "user_id": user_id,
                "status": status,
                "error": error,
                "report_path": report_path,
            }, default=str),
            fingerprint,
            omodul_name,
        )


async def recent_events(
    *,
    conn: asyncpg.Connection,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    service: str | None = None,
    hours: int = 24,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch recent events for a tenant."""
    if service:
        rows = await conn.fetch(
            """
            SELECT id, ts, event_type, severity, payload, omodul_kind, autoheal_plugin, trace_id
            FROM event_trail
            WHERE org_id = $1 AND project_id = $2 AND service = $3
              AND ts > now() - ($4 || ' hours')::interval
            ORDER BY ts DESC
            LIMIT $5
            """,
            org_id,
            project_id,
            service,
            str(hours),
            limit,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, ts, event_type, severity, payload, omodul_kind, autoheal_plugin, trace_id
            FROM event_trail
            WHERE org_id = $1 AND project_id = $2
              AND ts > now() - ($3 || ' hours')::interval
            ORDER BY ts DESC
            LIMIT $4
            """,
            org_id,
            project_id,
            str(hours),
            limit,
        )
    return [dict(r) for r in rows]


async def causal_chain(
    *,
    conn: asyncpg.Connection,
    event_id: uuid.UUID,
    max_depth: int = 20,
) -> list[dict[str, Any]]:
    """Walk parent_id chain from event_id upward."""
    rows = await conn.fetch(
        """
        WITH RECURSIVE chain AS (
            SELECT id, parent_id, event_type, payload, ts, 0 AS depth
            FROM event_trail
            WHERE id = $1
            UNION ALL
            SELECT e.id, e.parent_id, e.event_type, e.payload, e.ts, c.depth + 1
            FROM event_trail e
            INNER JOIN chain c ON e.id = c.parent_id
            WHERE c.depth < $2
        )
        SELECT * FROM chain ORDER BY depth ASC
        """,
        event_id,
        max_depth,
    )
    return [dict(r) for r in rows]
