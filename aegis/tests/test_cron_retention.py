"""Tests for the retention loop + storage guard (DESIGN §7 / I6 / C-I6).

retention 循环按登记表分批删过期遥测(有界写入者),存储守卫在生产盘越阈时大声告警。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.orchestration import cron
from aegis.server.persistence.retention import RETENTION, STORAGE_GUARD_PERCENT


def _mk_result(**attrs):
    m = MagicMock()
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.mark.asyncio
async def test_retention_loop_prunes_every_registered_table():
    """一轮迭代应对每张登记表调 retention_prune,并执行一次 disk_usage 存储守卫。"""
    calls: list[tuple[str, dict]] = []

    async def fake_to_thread(fn, **kw):
        calls.append((fn.__name__, kw))
        if fn.__name__ == "disk_usage":
            return _mk_result(over_threshold=False, used_percent=10.0)
        return _mk_result(deleted_rows=0, table=kw.get("table"))

    # 首个 sleep(初始抖动)放行,第二个(轮末)抛 Cancelled 退出 while True
    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cron._retention_loop()

    prune_calls = [c for c in calls if c[0] == "retention_prune"]
    assert len(prune_calls) == len(RETENTION)  # 每张登记表都被 prune
    # 传参与登记表一致(表名+ts列)
    pruned_tables = {c[1]["table"] for c in prune_calls}
    assert pruned_tables == {str(e["table"]) for e in RETENTION}
    assert any(c[0] == "disk_usage" for c in calls)  # 存储守卫执行


@pytest.mark.asyncio
async def test_storage_guard_breach_logs_warning():
    """disk_usage.over_threshold=True → 记 storage_guard_breach 告警。"""

    async def fake_to_thread(fn, **kw):
        if fn.__name__ == "disk_usage":
            return _mk_result(over_threshold=True, used_percent=88.5)
        return _mk_result(deleted_rows=0, table=kw.get("table"))

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
        patch.object(cron.log, "warning") as m_warn,
    ):
        with pytest.raises(asyncio.CancelledError):
            await cron._retention_loop()

    assert any("storage_guard_breach" in str(c.args) for c in m_warn.call_args_list)


@pytest.mark.asyncio
async def test_retention_prune_error_does_not_abort_loop():
    """单表 prune 抛错不应阻断后续表或存储守卫。"""
    seen: list[str] = []

    async def fake_to_thread(fn, **kw):
        seen.append(fn.__name__)
        if fn.__name__ == "retention_prune" and kw.get("table") == str(RETENTION[0]["table"]):
            raise RuntimeError("boom")
        if fn.__name__ == "disk_usage":
            return _mk_result(over_threshold=False, used_percent=10.0)
        return _mk_result(deleted_rows=0, table=kw.get("table"))

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cron._retention_loop()

    # 首表抛错后,其余表仍尝试 + 存储守卫仍执行
    assert seen.count("retention_prune") == len(RETENTION)
    assert "disk_usage" in seen


def test_retention_registry_is_bounded_and_sane():
    """登记表非空、天数为正、存储阈值合理 —— 结构不变式。"""
    assert RETENTION, "retention 登记表不得为空"
    for e in RETENTION:
        assert int(e["retain_days"]) > 0  # type: ignore[call-overload]
        assert e["table"] and e["ts_column"]
    assert 0 < STORAGE_GUARD_PERCENT < 100
