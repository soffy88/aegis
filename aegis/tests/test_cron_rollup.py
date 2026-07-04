"""Tests for the metric rollup loop (DESIGN §4.2/§7 downsampling)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.orchestration import cron


@pytest.mark.asyncio
async def test_rollup_loop_calls_downsample_with_expected_args():
    captured = {}

    async def fake_to_thread(fn, **kw):
        captured.update(kw)
        captured["fn"] = fn.__name__
        return MagicMock(rows_written=42)

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cron._rollup_loop()

    assert captured["fn"] == "metric_downsample_rollup"
    assert captured["source_table"] == "agent_metrics"
    assert captured["dest_table"] == "agent_metrics_rollup_1h"
    assert captured["ts_column"] == "ts" and captured["value_column"] == "value"
    assert captured["agg"] == "avg" and captured["bucket_seconds"] == 3600
    assert captured["label_columns"] == ["metric_name", "hostname"]


@pytest.mark.asyncio
async def test_rollup_error_does_not_crash_loop():
    async def fake_to_thread(fn, **kw):
        raise RuntimeError("db down")

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
        patch.object(cron.log, "warning") as m_warn,
    ):
        with pytest.raises(asyncio.CancelledError):
            await cron._rollup_loop()
    assert any("metric_rollup_error" in str(c.args) for c in m_warn.call_args_list)


def test_rollup_is_supervised():
    assert "rollup" in cron._SUPERVISED_LOOPS


def test_rollup_registered_in_retention():
    from aegis.server.persistence.retention import RETENTION

    tables = {str(e["table"]) for e in RETENTION}
    assert "agent_metrics_rollup_1h" in tables  # rollup 表也受保留约束(90d)
