"""Tests for aegis_agent._loop.run_loop."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from aegis_agent._loop import run_loop


@pytest.mark.asyncio
async def test_loop_calls_collect_and_report_once() -> None:
    """With stop_event already set, loop runs one cycle then exits."""
    stop = asyncio.Event()
    stop.set()

    collect = MagicMock(return_value=[{"name": "cpu_percent", "value": 10.0}])
    report = MagicMock(return_value=True)

    await run_loop(collect_fn=collect, report_fn=report, interval_seconds=0, stop_event=stop)

    collect.assert_called_once()
    report.assert_called_once_with([{"name": "cpu_percent", "value": 10.0}])


@pytest.mark.asyncio
async def test_loop_passes_metrics_to_reporter() -> None:
    stop = asyncio.Event()
    stop.set()

    metrics = [{"name": "ram_percent", "value": 55.0, "unit": "%", "tags": {}}]
    collect = MagicMock(return_value=metrics)
    report = MagicMock(return_value=True)

    await run_loop(collect_fn=collect, report_fn=report, interval_seconds=0, stop_event=stop)

    report.assert_called_once_with(metrics)


@pytest.mark.asyncio
async def test_loop_does_not_raise_on_collect_error() -> None:
    """A collect error is logged and swallowed; loop exits cleanly."""
    stop = asyncio.Event()
    stop.set()

    collect = MagicMock(side_effect=RuntimeError("oprim unavailable"))
    report = MagicMock(return_value=True)

    await run_loop(collect_fn=collect, report_fn=report, interval_seconds=0, stop_event=stop)

    report.assert_not_called()


@pytest.mark.asyncio
async def test_loop_does_not_raise_on_report_error() -> None:
    """A report error is logged and swallowed; loop exits cleanly."""
    stop = asyncio.Event()
    stop.set()

    collect = MagicMock(return_value=[{"name": "cpu_percent", "value": 1.0}])
    report = MagicMock(side_effect=RuntimeError("backend down"))

    await run_loop(collect_fn=collect, report_fn=report, interval_seconds=0, stop_event=stop)

    collect.assert_called_once()


@pytest.mark.asyncio
async def test_loop_runs_multiple_cycles() -> None:
    """Without stop_event, loop runs until cancelled after N cycles."""
    call_count = 0

    def _collect() -> list:
        nonlocal call_count
        call_count += 1
        return []

    async def _cancel_after_two() -> None:
        while call_count < 2:
            await asyncio.sleep(0)

    collect = MagicMock(side_effect=_collect)
    report = MagicMock(return_value=True)

    task = asyncio.create_task(run_loop(collect_fn=collect, report_fn=report, interval_seconds=0))
    await _cancel_after_two()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert collect.call_count >= 2
