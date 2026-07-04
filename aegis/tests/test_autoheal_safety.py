"""Tests for the §5.3 autoheal safety layer: kill switch + flapping + rate limit."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.services import autoheal_policy as ap


def _policy(**kw):
    base = dict(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="svc-restart",
        target_container="test-svc",
        trigger_metric="probe_up",
        trigger_operator="<",
        trigger_threshold=1.0,
        action="restart",
        dry_run=False,
        cooldown_seconds=300,
        docker_host=None,
        last_triggered_at=None,
    )
    base.update(kw)
    return base


def _conn(policy, metric_value, *, flag_enabled=False):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[policy], [{"value": metric_value}]])
    conn.execute = AsyncMock()
    # kill-switch 读取:返回 flag 行(或 None)
    conn.fetchrow = AsyncMock(return_value={"enabled": flag_enabled} if flag_enabled else None)
    return conn


@pytest.fixture(autouse=True)
def _reset_state():
    ap._HEAL_HISTORY.clear()
    ap._RECENT_ACTIONS.clear()
    yield
    ap._HEAL_HISTORY.clear()
    ap._RECENT_ACTIONS.clear()


@pytest.mark.asyncio
async def test_config_kill_switch_halts_all_autoheal():
    conn = _conn(_policy(), metric_value=0.0)
    cfg = MagicMock()
    cfg.autoheal_enabled = False
    with (
        patch("aegis.server.runtime.config.get_settings", return_value=cfg),
        patch("obase.docker.docker_container_restart") as restart,
    ):
        actions = await ap.run_autoheal_policies(conn)
    assert actions == [] and restart.call_count == 0
    conn.fetch.assert_not_awaited()  # config 急停 → 连策略都不查


@pytest.mark.asyncio
async def test_runtime_kill_switch_flag_halts_all_autoheal():
    conn = _conn(_policy(), metric_value=0.0, flag_enabled=True)
    with (
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()),
        patch("obase.docker.docker_container_restart") as restart,
    ):
        actions = await ap.run_autoheal_policies(conn)
    assert actions == [] and restart.call_count == 0  # 运行时 flag 置位 → 全停


@pytest.mark.asyncio
async def test_flapping_target_suppressed_and_escalated():
    """目标近窗口已自愈达阈值 → 抖动:不再重启,记 critical 升级。"""
    now = ap._utcnow()
    ap._HEAL_HISTORY["test-svc"] = [now, now]  # 默认 threshold=2 → 抖动
    conn = _conn(_policy(), metric_value=0.0)
    with (
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()) as ins,
        patch("obase.docker.docker_container_restart") as restart,
    ):
        actions = await ap.run_autoheal_policies(conn)
    assert restart.call_count == 0  # 抖动 → 停手
    assert actions[0]["suppressed"] == "flapping" and actions[0]["ok"] is False
    assert ins.await_args.kwargs["severity"] == "critical"
    assert "flapping" in ins.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_rate_limit_suppresses_real_action():
    """全局窗口内动作已达上限 → 限流:跳过真实重启。"""
    now = ap._utcnow()
    ap._RECENT_ACTIONS.extend([now] * 10)  # 默认 max=10 → 已满
    conn = _conn(_policy(), metric_value=0.0)
    with (
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()),
        patch("obase.docker.docker_container_restart") as restart,
    ):
        actions = await ap.run_autoheal_policies(conn)
    assert restart.call_count == 0
    assert actions[0]["suppressed"] == "rate_limit"


@pytest.mark.asyncio
async def test_successful_restart_records_history_and_action():
    """真实重启成功 → 记入抖动历史 + 全局限流计数。"""
    conn = _conn(_policy(), metric_value=0.0)
    with (
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()),
        patch("obase.docker.docker_container_restart") as restart,
    ):
        actions = await ap.run_autoheal_policies(conn)
    restart.assert_called_once()
    assert actions[0]["ok"] is True and actions[0]["suppressed"] is None
    assert len(ap._HEAL_HISTORY["test-svc"]) == 1  # 记入历史
    assert len(ap._RECENT_ACTIONS) == 1  # 记入限流


def test_rate_limited_prunes_expired():
    """_rate_limited 剪掉窗口外时刻。"""
    from datetime import timedelta

    now = ap._utcnow()
    ap._RECENT_ACTIONS[:] = [now - timedelta(hours=2), now]  # 一个过期一个新
    limited = ap._rate_limited(now, max_actions=10, window_seconds=3600)
    assert limited is False
    assert len(ap._RECENT_ACTIONS) == 1  # 过期项被剪
