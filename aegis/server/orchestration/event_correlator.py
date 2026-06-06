"""Event-trail causal-chain correlator.

Runs every 5 min (cron). Finds events without parent_id and attempts to
link them using oskill.event_trail_correlate (heuristic + time-window match).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import asyncpg
from oskill import event_trail_correlate

log = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 0.6
_WINDOW_SEC = 300  # 5-minute correlation window
_LOOKBACK_HOURS = 2  # only correlate recent events


async def correlate_org_events(
    *,
    conn: asyncpg.Connection,
    org_id: uuid.UUID,
) -> list[str]:
    """Link orphan events to their causal ancestors.

    Returns list of event IDs that were updated with a parent_id.
    """
    rows = await conn.fetch(
        """
        SELECT id, ts, event_type, severity, service, payload, trace_id,
               parent_id, root_cause_id, omodul_kind, autoheal_plugin
        FROM event_trail
        WHERE org_id = $1
          AND ts > now() - interval '2 hours'
        ORDER BY ts ASC
        """,
        org_id,
    )
    if not rows:
        return []

    all_events: list[dict[str, Any]] = [dict(r) for r in rows]
    loop = asyncio.get_event_loop()
    updated: list[str] = []

    for ev in all_events:
        # Skip events already linked
        if ev.get("parent_id") is not None:
            continue

        ev_id = str(ev["id"])

        result = await loop.run_in_executor(
            None,
            lambda e=ev_id: event_trail_correlate(
                target_event_id=e,
                all_events=all_events,
                time_window_sec=_WINDOW_SEC,
            ),
        )

        if result.confidence < _CONFIDENCE_THRESHOLD:
            continue
        if not result.causally_related:
            continue

        # Pick the earliest causally-related event as the root
        root = result.causally_related[0]
        root_id = str(root["id"])
        if root_id == ev_id:
            continue

        await conn.execute(
            """
            UPDATE event_trail
               SET parent_id     = $2::uuid,
                   root_cause_id = $3::uuid
             WHERE id = $1::uuid
               AND parent_id IS NULL
            """,
            ev_id,
            root_id,
            root_id,
        )
        updated.append(ev_id)
        log.info(
            "correlated event=%s → root=%s confidence=%.2f",
            ev_id,
            root_id,
            result.confidence,
        )

    return updated


async def run_correlator_for_all_orgs(conn: asyncpg.Connection) -> None:
    """Cron entry-point: correlate events for every active org."""
    org_rows = await conn.fetch("SELECT id FROM orgs")
    for row in org_rows:
        try:
            updated = await correlate_org_events(conn=conn, org_id=row["id"])
            if updated:
                log.info("correlator org=%s updated=%d", row["id"], len(updated))
        except Exception as exc:
            log.warning("correlator failed org=%s err=%s", row["id"], exc)
