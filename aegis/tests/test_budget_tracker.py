"""Tests for BudgetTracker (SF1: coverage 68% → ≥85%)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest import mock

import pytest

from aegis.server.dispatch.budget_tracker import BudgetTracker


def _mock_redis() -> mock.AsyncMock:
    r = mock.AsyncMock()
    r.get.return_value = None
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
async def test_deduct_calls_incrbyfloat_and_expire() -> None:
    """deduct() increments Redis key and sets TTL."""
    redis = _mock_redis()
    tracker = BudgetTracker(redis)
    await tracker.deduct("user_a", 10.0)

    redis.incrbyfloat.assert_called_once()
    key_arg = redis.incrbyfloat.call_args[0][0]
    assert "user_a" in key_arg
    assert redis.incrbyfloat.call_args[0][1] == 10.0
    redis.expire.assert_called_once_with(key_arg, 32 * 86400)


@pytest.mark.asyncio
async def test_deduct_zero_skips() -> None:
    """deduct(0) does nothing (early return)."""
    redis = _mock_redis()
    tracker = BudgetTracker(redis)
    await tracker.deduct("user_b", 0.0)
    redis.incrbyfloat.assert_not_called()


@pytest.mark.asyncio
async def test_monthly_key_format() -> None:
    """Key includes user_id and current YYYYMM."""
    redis = _mock_redis()
    tracker = BudgetTracker(redis)
    key = tracker._key("user_c")
    ym = datetime.now(UTC).strftime("%Y%m")
    assert f"aegis:budget:user_c:{ym}" == key
