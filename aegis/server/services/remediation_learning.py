"""Remediation learning loop — record what fixed what, recall it for planning.

Every executed remediation (runbook / autoheal plugin) records an outcome keyed by a
normalized *symptom*. When the Brain plans a response to a new alert, the historical
success rates for that symptom are injected into the planner context so it favours
remediations that actually worked before ("restart-x succeeded 9/10 times").
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

log = logging.getLogger(__name__)


def symptom_key(raw: str) -> str:
    """Normalize a symptom/trigger to a stable key.

    Runbook triggers ('alert:nginx_unhealthy') and alert names ('nginx_unhealthy')
    collapse to the same key so record + recall align.
    """
    return (raw or "").split(":")[-1].strip().lower()


async def record_outcome(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    symptom: str,
    remediation: str,
    success: bool,
    source: str = "runbook",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record one remediation outcome. Best-effort (never raises)."""
    try:
        await conn.execute(
            "INSERT INTO remediation_outcomes"
            " (org_id, symptom_key, remediation, success, source, metadata)"
            " VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
            org_id,
            symptom_key(symptom),
            remediation,
            success,
            source,
            json.dumps(metadata or {}),
        )
    except Exception as exc:  # noqa: BLE001 — learning must not break remediation
        log.warning("outcome_record_failed symptom=%s err=%s", symptom, exc)


async def success_stats(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    symptom: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Per-remediation success stats for a symptom, best first.

    Returns [{remediation, successes, total, success_rate}], ordered by success_rate
    then sample size. Empty list if nothing learned yet.
    """
    rows = await conn.fetch(
        "SELECT remediation,"
        "       count(*) FILTER (WHERE success) AS successes,"
        "       count(*) AS total"
        "  FROM remediation_outcomes"
        " WHERE org_id = $1 AND symptom_key = $2"
        " GROUP BY remediation"
        " ORDER BY (count(*) FILTER (WHERE success))::float / count(*) DESC, count(*) DESC"
        " LIMIT $3",
        org_id,
        symptom_key(symptom),
        limit,
    )
    return [
        {
            "remediation": r["remediation"],
            "successes": r["successes"],
            "total": r["total"],
            "success_rate": round(r["successes"] / r["total"], 3) if r["total"] else 0.0,
        }
        for r in rows
    ]
