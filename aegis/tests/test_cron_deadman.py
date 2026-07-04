"""Tests for loop liveness + dead-man switch (DESIGN §6 L1 / §4.2 supervision).

_tick 每轮标记循环存活;_deadman_loop 评估卡死(deadman_evaluate),健康才发外部心跳,
卡死则抑制心跳 → 外部 watcher 触发("谁看门人")。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.orchestration import cron


@pytest.mark.asyncio
async def test_tick_marks_loop_alive():
    cron._LOOP_LAST_SEEN.pop("scrape", None)
    before = cron._utcnow()
    with patch("asyncio.sleep", AsyncMock()):
        await cron._tick("scrape", 15)
    assert "scrape" in cron._LOOP_LAST_SEEN
    assert cron._LOOP_LAST_SEEN["scrape"] >= before


def _settings(url: str = "", timeout: float = 5.0):
    s = MagicMock()
    s.deadman_heartbeat_url = url
    s.deadman_heartbeat_timeout_sec = timeout
    return s


async def _run_one_deadman_iter(settings, to_thread_fn):
    """跑 _deadman_loop 一轮后退出(第二个 sleep 抛 Cancelled)。"""
    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("aegis.server.runtime.config.get_settings", return_value=settings),
        patch("asyncio.to_thread", side_effect=to_thread_fn),
        patch("asyncio.sleep", sleep_mock),
    ):
        with pytest.raises(asyncio.CancelledError):
            await cron._deadman_loop()


@pytest.mark.asyncio
async def test_healthy_loops_emit_external_heartbeat():
    """无卡死 + 配了 URL → 发外部心跳。"""
    cron._LOOP_LAST_SEEN.clear()  # 全 never_seen(startup_grace 兜住,不算卡死)
    calls: list[str] = []

    async def fake_to_thread(fn, **kw):
        calls.append(fn.__name__)
        return MagicMock(delivered=True, status_code=200, error=None)

    await _run_one_deadman_iter(_settings(url="https://hc.example/ping"), fake_to_thread)
    assert "heartbeat_emit" in calls  # 健康 → 心跳发出


@pytest.mark.asyncio
async def test_stalled_loop_suppresses_heartbeat_and_logs():
    """某循环曾见但静默超阈 → 卡死:抑制心跳 + error 日志。"""
    cron._LOOP_LAST_SEEN.clear()
    # anomaly 循环 last_seen 远在过去(interval=60,grace≈360;设 1 小时前必判卡死)
    cron._LOOP_LAST_SEEN["anomaly"] = cron._utcnow() - timedelta(hours=1)
    calls: list[str] = []

    async def fake_to_thread(fn, **kw):
        calls.append(fn.__name__)
        return MagicMock(delivered=True)

    with (
        patch.object(cron.log, "error") as m_err,
        patch.object(cron.log, "warning") as m_warn,
    ):
        await _run_one_deadman_iter(_settings(url="https://hc.example/ping"), fake_to_thread)

    assert "heartbeat_emit" not in calls  # 卡死 → 心跳被抑制
    assert any("loop_deadman_stalled" in str(c.args) for c in m_err.call_args_list)
    assert any("deadman_heartbeat_suppressed" in str(c.args) for c in m_warn.call_args_list)
    cron._LOOP_LAST_SEEN.clear()


@pytest.mark.asyncio
async def test_no_url_skips_external_heartbeat():
    """未配 URL → 外部死人禁用,不发心跳(degraded)。"""
    cron._LOOP_LAST_SEEN.clear()
    calls: list[str] = []

    async def fake_to_thread(fn, **kw):
        calls.append(fn.__name__)
        return MagicMock(delivered=True)

    await _run_one_deadman_iter(_settings(url=""), fake_to_thread)
    assert "heartbeat_emit" not in calls


def test_all_registered_loops_are_supervised():
    """每个进 gather 的 while-loop 都应在 _SUPERVISED_LOOPS 里(否则死人监督有盲区)。"""
    # _tick 的 name 集合应与 _SUPERVISED_LOOPS 键一致
    import inspect

    src = inspect.getsource(cron)
    ticked = set(__import__("re").findall(r'_tick\("(\w+)"', src))
    assert ticked == set(cron._SUPERVISED_LOOPS), (ticked, set(cron._SUPERVISED_LOOPS))
