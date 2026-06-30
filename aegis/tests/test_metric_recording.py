"""Tests for derived CPU% recording (rate of the cAdvisor CPU counter)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from aegis.server.services.metric_recording import record_container_cpu_percent

_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_computes_cpu_percent_from_counter_rate():
    # one container: +10 CPU-seconds over 10s wallclock -> 100% of one core
    tags = json.dumps({"cpu": "total", "id": "/docker/abc", "source": "cadvisor"})
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"tags": tags, "value": 110.0, "ts": _NOW},
        {"tags": tags, "value": 100.0, "ts": _NOW - timedelta(seconds=10)},
    ])
    conn.executemany = AsyncMock()

    n = await record_container_cpu_percent(conn)

    assert n == 1
    rows = conn.executemany.await_args.args[1]
    host, metric, pct, unit, _tags = rows[0]
    assert metric == "container_cpu_percent" and unit == "%"
    assert abs(pct - 100.0) < 1e-6


@pytest.mark.asyncio
async def test_counter_reset_clamped_to_zero():
    tags = json.dumps({"cpu": "total", "id": "/docker/x", "source": "cadvisor"})
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"tags": tags, "value": 5.0, "ts": _NOW},                       # reset (lower)
        {"tags": tags, "value": 900.0, "ts": _NOW - timedelta(seconds=10)},
    ])
    conn.executemany = AsyncMock()
    await record_container_cpu_percent(conn)
    assert conn.executemany.await_args.args[1][0][2] == 0.0  # clamped


@pytest.mark.asyncio
async def test_single_sample_series_skipped():
    tags = json.dumps({"cpu": "total", "id": "/docker/y"})
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"tags": tags, "value": 1.0, "ts": _NOW}])
    conn.executemany = AsyncMock()
    n = await record_container_cpu_percent(conn)
    assert n == 0
    conn.executemany.assert_not_awaited()
