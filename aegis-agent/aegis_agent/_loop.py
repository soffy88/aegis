"""60-second collection loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)


async def run_loop(
    collect_fn: Callable[[], Awaitable[list[dict[str, Any]]]],
    report_fn: Callable[[list[dict[str, Any]]], bool],
    interval_seconds: int = 60,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Collect and report metrics every interval_seconds.

    Args:
        collect_fn: Async callable that returns a list of metric point dicts.
        report_fn: Callable that POSTs metrics, returns True on success.
        interval_seconds: Sleep duration between collection cycles.
        stop_event: Optional event to signal graceful shutdown.
    """
    log.info("aegis_agent_loop_start interval=%ds", interval_seconds)
    while True:
        try:
            metrics = await collect_fn()
            await asyncio.to_thread(report_fn, metrics)
        except Exception as exc:  # noqa: BLE001
            log.error("aegis_agent_loop_error: %s", exc)

        if stop_event is not None and stop_event.is_set():
            break

        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                break
            else:
                await asyncio.sleep(interval_seconds)
        except TimeoutError:
            pass  # normal — interval elapsed, continue loop
