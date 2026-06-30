"""Periodic anomaly scan over agent_metrics → metric_anomalies.

For each (hostname, metric_name) active in the recent window, run the EWMA detector on
its ordered series and persist any anomaly on the latest point. A per-series cooldown
prevents a sustained anomaly from inserting a row every tick.
"""

from __future__ import annotations

import logging

import asyncpg

from aegis.server.services.anomaly import ewma_anomaly

log = logging.getLogger(__name__)

_LOOKBACK_HOURS = 6
_MIN_SAMPLES = 8
_COOLDOWN_MINUTES = 15


async def scan_anomalies(conn: asyncpg.Connection) -> int:
    """Detect + persist anomalies for recently-active metric series. Returns count."""
    series = await conn.fetch(
        "SELECT DISTINCT hostname, metric_name FROM agent_metrics"
        " WHERE ts >= now() - ($1::int * interval '1 hour')",
        _LOOKBACK_HOURS,
    )
    found = 0
    for s in series:
        host, metric = s["hostname"], s["metric_name"]
        rows = await conn.fetch(
            "SELECT value FROM agent_metrics"
            " WHERE hostname = $1 AND metric_name = $2"
            "   AND ts >= now() - ($3::int * interval '1 hour')"
            " ORDER BY ts ASC",
            host,
            metric,
            _LOOKBACK_HOURS,
        )
        values = [float(r["value"]) for r in rows]
        if len(values) < _MIN_SAMPLES:
            continue
        result = ewma_anomaly(values)
        if result is None or not result.is_anomaly:
            continue
        # Cooldown: skip if we already flagged this series recently.
        recent = await conn.fetchval(
            "SELECT 1 FROM metric_anomalies"
            " WHERE hostname = $1 AND metric_name = $2"
            "   AND detected_at >= now() - ($3::int * interval '1 minute') LIMIT 1",
            host,
            metric,
            _COOLDOWN_MINUTES,
        )
        if recent:
            continue
        await conn.execute(
            "INSERT INTO metric_anomalies (hostname, metric_name, value, baseline, score)"
            " VALUES ($1, $2, $3, $4, $5)",
            host,
            metric,
            result.value,
            result.baseline,
            result.score,
        )
        found += 1
        log.info(
            "anomaly_detected host=%s metric=%s value=%.3f baseline=%.3f z=%.2f",
            host,
            metric,
            result.value,
            result.baseline,
            result.score,
        )
    return found
