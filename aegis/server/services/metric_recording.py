"""Derived-metric recording (rate computation).

cAdvisor exposes CPU as a cumulative counter (`container_cpu_usage_seconds_total`),
which a simple threshold rule can't use. This computes a per-container CPU% gauge
(`container_cpu_percent`, % of one core) from the rate of that counter and writes it
back into `agent_metrics`, so the existing per-series alert evaluation can threshold
on it. Run periodically from the orchestration cron.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

_CPU_COUNTER = "container_cpu_usage_seconds_total"
_CPU_GAUGE = "container_cpu_percent"


def _as_dict(tags: Any) -> dict[str, Any]:
    if isinstance(tags, dict):
        return tags
    try:
        return json.loads(tags)
    except Exception:  # noqa: BLE001
        return {}


async def record_container_cpu_percent(conn: asyncpg.Connection) -> int:
    """Compute per-container CPU% from the CPU counter's rate; insert as a gauge.

    Returns the number of gauge rows written. For each container series (cpu=total)
    it takes the two most recent samples and computes (Δseconds / Δwallclock) * 100,
    i.e. percent of a single core. Negative deltas (counter reset / container
    restart) are clamped to 0.
    """
    rows = await conn.fetch(
        """
        SELECT tags, value, ts
        FROM agent_metrics
        WHERE metric_name = $1
          AND ts > now() - interval '3 minutes'
          AND tags->>'cpu' = 'total'
        ORDER BY tags->>'id', ts DESC
        """,
        _CPU_COUNTER,
    )

    # group by container id, newest first (query already ordered)
    by_id: dict[str, list[tuple[float, Any, Any]]] = {}
    for r in rows:
        tags = _as_dict(r["tags"])
        cid = tags.get("id")
        if not cid:
            continue
        by_id.setdefault(cid, []).append((r["value"], r["ts"], r["tags"]))

    inserts: list[tuple[str, str, float, str, str]] = []
    for samples in by_id.values():
        if len(samples) < 2:
            continue
        (v2, t2, tags), (v1, t1, _) = samples[0], samples[1]
        dt = (t2 - t1).total_seconds()
        if dt <= 0:
            continue
        pct = max(0.0, (v2 - v1) / dt * 100.0)
        tags_json = tags if isinstance(tags, str) else json.dumps(tags)
        inserts.append(("cadvisor", _CPU_GAUGE, pct, "%", tags_json))

    if inserts:
        await conn.executemany(
            "INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags)"
            " VALUES ($1, $2, $3, $4, $5::jsonb)",
            inserts,
        )
    return len(inserts)
