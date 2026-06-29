"""Capacity forecaster — predicts resource exhaustion and fires alerts.

Cron 1h: fetch agent_metrics samples, run oskill.compute_capacity_forecast,
fire alert via platform_alerter if breach predicted within 30 days.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncpg
from oskill import CapacityForecastResult, compute_capacity_forecast

from aegis.server.runtime.config import get_settings

log = logging.getLogger(__name__)


async def check_capacity_metrics(
    *,
    conn: asyncpg.Connection,
    alerter: Any | None = None,
) -> list[CapacityForecastResult]:
    """Forecast resource capacity; returns list of metrics predicted to breach.

    Thresholds, minimum samples and the forecast horizon come from AegisSettings
    (AEGIS_CAPACITY_*), so operators can tune them without a code change.

    alerter: optional object with a .fire(metric, result) method for alert delivery.
    """
    cfg = get_settings()
    min_samples = cfg.capacity_min_samples
    thresholds = cfg.capacity_metric_thresholds
    default_threshold = cfg.capacity_default_threshold
    breach_days_warn = cfg.capacity_breach_days_warn
    rows = await conn.fetch(
        """
        SELECT metric_name, value, unit
        FROM agent_metrics
        WHERE ts > now() - interval '48 hours'
        ORDER BY metric_name, ts ASC
        """,
    )
    if not rows:
        return []

    # Group samples by metric_name
    by_metric: dict[str, list[float]] = {}
    for row in rows:
        by_metric.setdefault(row["metric_name"], []).append(float(row["value"]))

    loop = asyncio.get_event_loop()
    breaching: list[CapacityForecastResult] = []

    for metric_name, samples in by_metric.items():
        if len(samples) < min_samples:
            continue

        threshold = thresholds.get(metric_name, default_threshold)

        result: CapacityForecastResult = await loop.run_in_executor(
            None,
            lambda m=metric_name, s=samples, t=threshold: compute_capacity_forecast(
                metric_name=m,
                samples=s,
                threshold=t,
                forecast_steps=breach_days_warn,
            ),
        )

        if not result.will_breach_threshold:
            continue

        log.warning(
            "capacity_breach_predicted metric=%s breach_at=%s recommendation=%s",
            metric_name,
            result.breach_at_offset,
            result.recommendation,
        )
        breaching.append(result)

        if alerter is not None:
            try:
                alerter.fire(
                    metric=metric_name,
                    message=(
                        f"Capacity warning: {metric_name} will breach "
                        f"{threshold}% threshold in ~{result.breach_at_offset} steps. "
                        f"{result.recommendation}"
                    ),
                    severity="warning",
                    result=result.model_dump(),
                )
            except Exception as exc:
                log.warning("capacity alerter.fire failed metric=%s err=%s", metric_name, exc)

    return breaching


async def run_capacity_check(conn: asyncpg.Connection, alerter: Any | None = None) -> None:
    """Cron entry-point: run capacity check and log summary."""
    breaching = await check_capacity_metrics(conn=conn, alerter=alerter)
    if breaching:
        log.warning("capacity_check_complete breaching_metrics=%d", len(breaching))
    else:
        log.info("capacity_check_complete no_breaches")
