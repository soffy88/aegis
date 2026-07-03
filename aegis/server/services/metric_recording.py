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

_NODE_CPU_COUNTER = "node_cpu_seconds_total"
_NODE_CPU_GAUGE = "node_cpu_percent"
_NODE_MEM_GAUGE = "node_memory_used_percent"
_NODE_HOST = "node-exporter"


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


async def record_node_percentages(conn: asyncpg.Connection) -> int:
    """Derive whole-host gauges from scraped node_exporter counters/gauges.

    - node_cpu_percent: 100 * (1 - Σidle / Σtotal) over the rate of
      node_cpu_seconds_total across all (cpu, mode) series.
    - node_memory_used_percent: 100 * (1 - MemAvailable / MemTotal).

    node_exporter only exposes raw counters/gauges; the dashboard's "整机 CPU/内存"
    meters read these two derived gauges. Written under the node-exporter hostname.
    Returns the number of gauge rows written (0-2).
    """
    inserts: list[tuple[str, str, float, str, str]] = []

    # --- CPU: rate over node_cpu_seconds_total, aggregated across cores ---
    cpu_rows = await conn.fetch(
        """
        SELECT tags->>'cpu' AS cpu, tags->>'mode' AS mode, value, ts
        FROM agent_metrics
        WHERE metric_name = $1
          AND ts > now() - interval '3 minutes'
        ORDER BY tags->>'cpu', tags->>'mode', ts DESC
        """,
        _NODE_CPU_COUNTER,
    )
    by_key: dict[tuple[str, str], list[tuple[float, Any]]] = {}
    for r in cpu_rows:
        if r["cpu"] is None or r["mode"] is None:
            continue
        by_key.setdefault((r["cpu"], r["mode"]), []).append((r["value"], r["ts"]))

    total_delta = 0.0
    idle_delta = 0.0
    for (_cpu, mode), samples in by_key.items():
        if len(samples) < 2:
            continue
        (v2, _t2), (v1, _t1) = samples[0], samples[1]
        d = max(0.0, v2 - v1)  # clamp counter resets
        total_delta += d
        if mode == "idle":
            idle_delta += d
    if total_delta > 0:
        pct = max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))
        inserts.append((_NODE_HOST, _NODE_CPU_GAUGE, pct, "%", "{}"))

    # --- Memory: (1 - MemAvailable / MemTotal) * 100 ---
    total = await conn.fetchval(
        "SELECT value FROM agent_metrics WHERE metric_name = 'node_memory_MemTotal_bytes'"
        " ORDER BY ts DESC LIMIT 1"
    )
    avail = await conn.fetchval(
        "SELECT value FROM agent_metrics WHERE metric_name = 'node_memory_MemAvailable_bytes'"
        " ORDER BY ts DESC LIMIT 1"
    )
    if total and avail is not None and total > 0:
        mem_pct = max(0.0, min(100.0, (1.0 - avail / total) * 100.0))
        inserts.append((_NODE_HOST, _NODE_MEM_GAUGE, mem_pct, "%", "{}"))

    if inserts:
        await conn.executemany(
            "INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags)"
            " VALUES ($1, $2, $3, $4, $5::jsonb)",
            inserts,
        )
    return len(inserts)
