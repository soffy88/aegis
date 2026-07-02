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

# Host-wide (node_exporter) totals. cAdvisor/container metrics answer "which single
# container is hottest"; these answer "how loaded is the whole machine" — total CPU%
# and total memory used across the host, including non-container processes.
_HOST = "node-exporter"
_CPU_IDLE_COUNTER = "node_cpu_seconds_total"  # counter per (cpu, mode)
_HOST_CPU_GAUGE = "node_cpu_percent"
_MEM_TOTAL = "node_memory_MemTotal_bytes"
_MEM_AVAIL = "node_memory_MemAvailable_bytes"
_HOST_MEM_USED = "node_memory_used_bytes"
_HOST_MEM_PCT = "node_memory_used_percent"


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


async def record_host_cpu_percent(conn: asyncpg.Connection) -> int:
    """Compute whole-host CPU busy% from node_exporter's idle CPU counter.

    node_cpu_seconds_total is a per-(cpu,mode) counter. Each CPU accrues 1 CPU-second
    of wallclock across all modes, so busy fraction = 1 - Δidle / (ncpu · Δwall). All
    rows of one scrape share a ts, so we group by ts, sum idle across CPUs, and diff
    the two most recent scrapes. Returns 1 if a gauge was written, else 0.
    """
    rows = await conn.fetch(
        """
        SELECT ts, value
        FROM agent_metrics
        WHERE metric_name = $1
          AND ts > now() - interval '3 minutes'
          AND tags->>'mode' = 'idle'
        ORDER BY ts DESC
        """,
        _CPU_IDLE_COUNTER,
    )
    by_ts: dict[Any, list[float]] = {}
    for r in rows:
        by_ts.setdefault(r["ts"], []).append(r["value"])
    tss = sorted(by_ts, reverse=True)
    if len(tss) < 2:
        return 0
    t2, t1 = tss[0], tss[1]
    ncpu = len(by_ts[t2])
    dt = (t2 - t1).total_seconds()
    if ncpu == 0 or dt <= 0:
        return 0
    idle_delta = sum(by_ts[t2]) - sum(by_ts[t1])
    busy = 100.0 * (1.0 - idle_delta / (ncpu * dt))
    busy = min(100.0, max(0.0, busy))  # clamp (counter reset / clock skew)
    await conn.execute(
        "INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags)"
        " VALUES ($1, $2, $3, $4, $5::jsonb)",
        _HOST,
        _HOST_CPU_GAUGE,
        busy,
        "%",
        "{}",
    )
    return 1


async def record_host_memory(conn: asyncpg.Connection) -> int:
    """Write whole-host memory used (bytes + percent) from node_exporter gauges.

    used = MemTotal - MemAvailable (the kernel's own reclaim-aware figure). Returns
    the number of gauge rows written (2), or 0 if the source gauges aren't present.
    """
    total_row = await conn.fetchrow(
        "SELECT value FROM agent_metrics WHERE metric_name = $1 ORDER BY ts DESC LIMIT 1",
        _MEM_TOTAL,
    )
    avail_row = await conn.fetchrow(
        "SELECT value FROM agent_metrics WHERE metric_name = $1 ORDER BY ts DESC LIMIT 1",
        _MEM_AVAIL,
    )
    if total_row is None or avail_row is None:
        return 0
    total = float(total_row["value"])
    if total <= 0:
        return 0
    used = max(0.0, total - float(avail_row["value"]))
    await conn.executemany(
        "INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags)"
        " VALUES ($1, $2, $3, $4, $5::jsonb)",
        [
            (_HOST, _HOST_MEM_USED, used, "bytes", "{}"),
            (_HOST, _HOST_MEM_PCT, used / total * 100.0, "%", "{}"),
        ],
    )
    return 2
