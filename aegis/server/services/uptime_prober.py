"""HTTP uptime probing.

Probes each enabled `uptime_targets` row (respecting its interval) via
oprim.network_http_health and records two gauges into `agent_metrics` —
`probe_up` (1.0 up / 0.0 down) and `probe_latency_ms` — keyed by the target name,
so the existing per-series alert evaluation can alert on `probe_up < 1` (and feed
the autoheal signal). Also stores last status on the target row for the UI.
"""

from __future__ import annotations

import json
import logging

import asyncpg
from oprim._network import network_http_health  # v3 not top-level

log = logging.getLogger(__name__)

_PROBE_TIMEOUT_SEC = 8


async def probe_due_targets(conn: asyncpg.Connection) -> int:
    """Probe every uptime target whose interval has elapsed. Returns count probed."""
    rows = await conn.fetch(
        """
        SELECT id, org_id, name, url, expected_status
        FROM uptime_targets
        WHERE enabled = TRUE
          AND (last_checked_at IS NULL
               OR last_checked_at <= now() - (interval_seconds * interval '1 second'))
        """
    )
    probed = 0
    for t in rows:
        up = False
        latency = 0
        err: str | None = None
        try:
            import asyncio  # noqa: PLC0415

            res = await asyncio.to_thread(
                network_http_health,
                url=t["url"],
                timeout_sec=_PROBE_TIMEOUT_SEC,
                expected_status=t["expected_status"],
            )
            up = bool(getattr(res, "healthy", False))
            latency = int(getattr(res, "elapsed_ms", 0) or 0)
            err = getattr(res, "error", None)
        except Exception as exc:  # noqa: BLE001 — a probe failure is a "down", not a crash
            err = str(exc)[:200]

        tags = json.dumps({"url": t["url"], "source": "uptime", "target": t["name"]})
        await conn.executemany(
            "INSERT INTO agent_metrics (hostname, metric_name, value, unit, tags)"
            " VALUES ($1, $2, $3, $4, $5::jsonb)",
            [
                (t["name"], "probe_up", 1.0 if up else 0.0, "", tags),
                (t["name"], "probe_latency_ms", float(latency), "ms", tags),
            ],
        )
        await conn.execute(
            "UPDATE uptime_targets SET last_up=$1, last_latency_ms=$2,"
            " last_checked_at=now(), last_error=$3 WHERE id=$4",
            up,
            latency,
            err,
            t["id"],
        )
        probed += 1

    if probed:
        log.debug("uptime_probe_tick probed=%d", probed)
    return probed
