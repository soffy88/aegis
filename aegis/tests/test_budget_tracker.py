"""Tests for BudgetTracker (SF1: coverage 68% → ≥85%)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest import mock

import pytest
from fakeredis import aioredis as fake_aioredis

from aegis.server.dispatch.budget_tracker import BudgetTracker


def _mock_redis() -> mock.AsyncMock:
    r = mock.AsyncMock()
    r.get.return_value = None
    pipe = mock.AsyncMock()
    pipe.watch = mock.AsyncMock()
    pipe.unwatch = mock.AsyncMock()
    pipe.get = mock.AsyncMock(return_value=None)
    pipe.multi = mock.Mock()
    pipe.incrbyfloat = mock.Mock()
    pipe.expire = mock.Mock()
    pipe.execute = mock.AsyncMock()
    pipe.__aenter__ = mock.AsyncMock(return_value=pipe)
    pipe.__aexit__ = mock.AsyncMock(return_value=False)
    r.pipeline = mock.Mock(return_value=pipe)
    r._pipe = pipe  # exposed for assertions
    return r


@pytest.mark.asyncio
async def test_has_budget_new_user() -> None:
    """New user (no Redis key) has full budget."""
    redis = _mock_redis()
    tracker = BudgetTracker(redis)
    assert await tracker.has_budget("user_new", 5.0) is True


@pytest.mark.asyncio
async def test_has_budget_exceeded() -> None:
    """User who spent over limit returns False."""
    redis = _mock_redis()
    redis.get.return_value = b"51.0"
    tracker = BudgetTracker(redis, monthly_limit_usd=50.0)
    assert await tracker.has_budget("user_over", 1.0) is False


@pytest.mark.asyncio
async def test_has_budget_at_limit() -> None:
    """User at exact limit cannot request more."""
    redis = _mock_redis()
    redis.get.return_value = b"50.0"
    tracker = BudgetTracker(redis, monthly_limit_usd=50.0)
    assert await tracker.has_budget("user_exact", 0.01) is False


@pytest.mark.asyncio
async def test_deduct_runs_atomic_check_and_increment_transaction() -> None:
    """deduct() applies the increment via a WATCH/MULTI/EXEC transaction."""
    redis = _mock_redis()
    tracker = BudgetTracker(redis)
    assert await tracker.deduct("user_a", 10.0) is True

    pipe = redis._pipe
    pipe.watch.assert_called_once()
    assert "user_a" in pipe.watch.call_args[0][0]
    pipe.multi.assert_called_once()
    pipe.incrbyfloat.assert_called_once()
    assert pipe.incrbyfloat.call_args[0][1] == 10.0
    pipe.expire.assert_called_once()
    assert pipe.expire.call_args[0][1] == 32 * 86400
    pipe.execute.assert_called_once()
    pipe.unwatch.assert_not_called()


@pytest.mark.asyncio
async def test_deduct_rejects_when_over_limit() -> None:
    """deduct() returns False and does not apply the increment if it would exceed the cap."""
    redis = _mock_redis()
    redis._pipe.get.return_value = b"45.0"
    tracker = BudgetTracker(redis, monthly_limit_usd=50.0)
    assert await tracker.deduct("user_over", 10.0) is False

    pipe = redis._pipe
    pipe.unwatch.assert_called_once()
    pipe.multi.assert_not_called()
    pipe.execute.assert_not_called()


@pytest.mark.asyncio
async def test_deduct_zero_skips() -> None:
    """deduct(0) does nothing (early return)."""
    redis = _mock_redis()
    tracker = BudgetTracker(redis)
    assert await tracker.deduct("user_b", 0.0) is True
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_deduct_concurrent_requests_never_exceed_limit() -> None:
    """Against a real (fake) Redis: concurrent deducts for the same user can't jointly
    blow past monthly_limit_usd — proves the WATCH/MULTI/EXEC retry loop is actually
    atomic, not just shaped like it (a mock can't catch a lost-update race)."""
    redis = fake_aioredis.FakeRedis()
    tracker = BudgetTracker(redis, monthly_limit_usd=50.0)

    results = await asyncio.gather(*(tracker.deduct("user_race", 10.0) for _ in range(10)))

    accepted = sum(results)
    assert accepted == 5  # exactly 50.0 / 10.0 requests can fit under the cap
    raw = await redis.get(tracker._key("user_race"))
    assert float(raw) == 50.0


@pytest.mark.asyncio
async def test_settle_refunds_unused_reservation() -> None:
    """reserve 5.0 then settle to actual 0.5 → counter reflects real spend (0.5)."""
    redis = fake_aioredis.FakeRedis()
    tracker = BudgetTracker(redis, monthly_limit_usd=50.0)

    assert await tracker.deduct("u", 5.0) is True  # reservation
    await tracker.settle("u", reserved_usd=5.0, actual_usd=0.5)

    assert float(await redis.get(tracker._key("u"))) == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_settle_zero_actual_full_refund() -> None:
    """A failed run (actual 0) refunds the entire reservation → counter back to 0."""
    redis = fake_aioredis.FakeRedis()
    tracker = BudgetTracker(redis, monthly_limit_usd=50.0)

    await tracker.deduct("u", 5.0)
    await tracker.settle("u", reserved_usd=5.0, actual_usd=0.0)

    assert float(await redis.get(tracker._key("u"))) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_settle_charges_overage_past_limit() -> None:
    """actual > reserved charges the difference even though it exceeds the reservation."""
    redis = fake_aioredis.FakeRedis()
    tracker = BudgetTracker(redis, monthly_limit_usd=50.0)

    await tracker.deduct("u", 5.0)
    await tracker.settle("u", reserved_usd=5.0, actual_usd=7.5)

    assert float(await redis.get(tracker._key("u"))) == pytest.approx(7.5)


@pytest.mark.asyncio
async def test_settle_noop_when_nothing_reserved() -> None:
    """reserved 0 (deduct early-returned) → settle does not touch Redis."""
    redis = _mock_redis()
    tracker = BudgetTracker(redis)
    await tracker.settle("u", reserved_usd=0.0, actual_usd=0.0)
    redis.incrbyfloat.assert_not_called()


@pytest.mark.asyncio
async def test_monthly_key_format() -> None:
    """Key includes user_id and current YYYYMM."""
    redis = _mock_redis()
    tracker = BudgetTracker(redis)
    key = tracker._key("user_c")
    ym = datetime.now(UTC).strftime("%Y%m")
    assert f"aegis:budget:user_c:{ym}" == key
