"""Incident correlation — cluster incoming signals into incidents.

Alerts and new error issues are deduplicated onto a single OPEN incident keyed by a
stable ``dedup_key`` (e.g. ``alert:<service>:<name>`` / ``error:<fingerprint>``), so a
flapping alert or recurring error grows one incident instead of spawning hundreds.

A partial unique index (org_id, dedup_key) WHERE status='open' enforces "one open
incident per key" at the DB level; this module handles the read-modify-write + the
race where two signals arrive concurrently.
"""

from __future__ import annotations

import logging
import uuid

import asyncpg

log = logging.getLogger(__name__)

# Higher number = more severe. Unknown severities sort lowest.
_SEVERITY_RANK = {"info": 0, "warning": 10, "warn": 10, "high": 20, "critical": 30}


def _rank(sev: str) -> int:
    return _SEVERITY_RANK.get((sev or "").lower(), 0)


_SEVERITY_RANK_SQL = (
    "CASE lower({col}) WHEN 'critical' THEN 30 WHEN 'high' THEN 20"
    " WHEN 'warning' THEN 10 WHEN 'warn' THEN 10 WHEN 'info' THEN 0 ELSE 0 END"
)


async def _attach(
    conn: asyncpg.Connection,
    *,
    incident_id: uuid.UUID,
    new_severity: str,
    event_id: uuid.UUID | None,
) -> None:
    # Rank comparison happens in SQL against the live row, not a Python-side value read
    # earlier — otherwise two concurrent signals can both read a stale low severity, both
    # decide to bump, and whichever UPDATE commits second clobbers a just-set higher one.
    await conn.execute(
        "UPDATE incidents SET event_count = event_count + 1, last_event_at = now(),"
        " severity = CASE WHEN "
        + _SEVERITY_RANK_SQL.format(col="severity")
        + " < "
        + _SEVERITY_RANK_SQL.format(col="$2::text")
        + " THEN $2 ELSE severity END"
        " WHERE id = $1",
        incident_id,
        new_severity,
    )
    if event_id is not None:
        await conn.execute(
            "INSERT INTO incident_events (incident_id, event_id) VALUES ($1, $2)"
            " ON CONFLICT DO NOTHING",
            incident_id,
            event_id,
        )


async def cluster_signal(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    dedup_key: str,
    title: str,
    severity: str = "warning",
    event_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, bool]:
    """Attach a signal to its open incident, creating one if none exists.

    Returns (incident_id, is_new). Severity is escalated to the max seen; event_count
    is bumped and the optional event_id is linked.
    """
    existing = await conn.fetchrow(
        "SELECT id FROM incidents WHERE org_id = $1 AND dedup_key = $2 AND status = 'open'",
        org_id,
        dedup_key,
    )
    if existing is not None:
        await _attach(
            conn,
            incident_id=existing["id"],
            new_severity=severity,
            event_id=event_id,
        )
        return existing["id"], False

    try:
        incident_id = await conn.fetchval(
            "INSERT INTO incidents (org_id, title, severity, status, dedup_key,"
            " event_count, last_event_at)"
            " VALUES ($1, $2, $3, 'open', $4, 1, now()) RETURNING id",
            org_id,
            title,
            severity,
            dedup_key,
        )
        if event_id is not None:
            await conn.execute(
                "INSERT INTO incident_events (incident_id, event_id) VALUES ($1, $2)"
                " ON CONFLICT DO NOTHING",
                incident_id,
                event_id,
            )
        log.info("incident_opened org=%s dedup=%s severity=%s", org_id, dedup_key, severity)
        return incident_id, True
    except asyncpg.UniqueViolationError:
        # Race: another signal opened the incident between our SELECT and INSERT.
        row = await conn.fetchrow(
            "SELECT id FROM incidents WHERE org_id = $1 AND dedup_key = $2 AND status = 'open'",
            org_id,
            dedup_key,
        )
        if row is None:  # pragma: no cover - it just got resolved; give up cleanly
            raise
        await _attach(
            conn,
            incident_id=row["id"],
            new_severity=severity,
            event_id=event_id,
        )
        return row["id"], False
