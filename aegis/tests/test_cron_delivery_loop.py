"""Tests for the webhook delivery cron loop (audit P0 #1).

Before this loop existed, `enqueue_event` queued deliveries that nothing ever
sent. These tests verify the loop drains the queue via `deliver_batch`, honors
the per-tick batch cap, and is registered in the cron gather set.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.orchestration import cron


class _StopLoop(Exception):
    pass


@asynccontextmanager
async def _fake_pool_acquire():
    yield MagicMock(name="conn")


def _patch_pool():
    pool = MagicMock()
    pool.acquire = lambda: _fake_pool_acquire()
    fake_persistence = MagicMock()
    fake_persistence.get_pool = lambda: pool
    return patch.dict(
        "sys.modules", {"aegis.server.persistence": fake_persistence}
    )


@pytest.mark.asyncio
async def test_delivery_loop_drains_until_empty():
    """One tick keeps calling deliver_batch until a batch reports zero work."""
    dispatcher = MagicMock()
    # two batches with work, then an empty batch → drain stops
    dispatcher.deliver_batch = AsyncMock(
        side_effect=[
            {"succeeded": 10, "failed_retry": 0, "dead_letter": 0},
            {"succeeded": 3, "failed_retry": 1, "dead_letter": 0},
            {"succeeded": 0, "failed_retry": 0, "dead_letter": 0},
        ]
    )

    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        # first sleep = initial jitter (allow); after the drain's inter-tick
        # sleep, stop the otherwise-infinite loop.
        if sleep_calls["n"] >= 2:
            raise _StopLoop

    with _patch_pool(), patch.object(
        cron, "_build_webhook_dispatcher", return_value=dispatcher
    ), patch.object(cron.asyncio, "sleep", side_effect=fake_sleep):
        with pytest.raises(_StopLoop):
            await cron._delivery_loop()

    assert dispatcher.deliver_batch.await_count == 3  # drained until empty


@pytest.mark.asyncio
async def test_delivery_loop_respects_batch_cap():
    """A never-empty queue must not wedge the loop: cap batches per tick."""
    dispatcher = MagicMock()
    dispatcher.deliver_batch = AsyncMock(
        return_value={"succeeded": 10, "failed_retry": 0, "dead_letter": 0}
    )

    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise _StopLoop

    with _patch_pool(), patch.object(
        cron, "_build_webhook_dispatcher", return_value=dispatcher
    ), patch.object(cron.asyncio, "sleep", side_effect=fake_sleep):
        with pytest.raises(_StopLoop):
            await cron._delivery_loop()

    # bounded by the per-tick cap, not infinite
    assert dispatcher.deliver_batch.await_count == cron._DELIVERY_DRAIN_BATCHES


@pytest.mark.asyncio
async def test_delivery_loop_registered_in_cron_main():
    """_cron_main must actually schedule the delivery loop."""
    scheduled: list[str] = []

    def _track(coro):
        scheduled.append(coro.__name__ if hasattr(coro, "__name__") else str(coro))
        coro.close()  # avoid 'coroutine was never awaited' warnings
        return None

    async def _fake_gather(*coros, **_kwargs):
        for c in coros:
            _track(c)

    with patch.object(cron.asyncio, "gather", side_effect=_fake_gather), patch.object(
        cron, "_acquire_loop_runner_role", AsyncMock(return_value=AsyncMock())
    ):
        await cron._cron_main(alerter=None)

    assert "_delivery_loop" in scheduled
