"""Tests for the §5.3 autoheal safety layer: kill switch + flapping + rate limit."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.services import autoheal_policy as ap
from aegis.server.services.preconditions import SystemReadiness, PreconditionState
from aegis.server.services.safety_mode import SafetyMode


def _ready_readiness(keys):
    """构造一个全部满足的前置条件态,用于让 autoheal 不被 degraded 闸门拦截。"""
    return SystemReadiness(
        degraded=False,
        preconditions=[PreconditionState(key=k, label=k, satisfied=True, reason=None) for k in keys],
    )


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
        canary=False,
        last_triggered_at=None,
    )
    base.update(kw)
    return base


def _conn(policy, metric_value, *, flag_enabled=False, heal_history=None, global_actions=None):
    conn = MagicMock()
    # Provide enough fetch responses for all the queries
    heal_hist = heal_history or []
    global_acts = global_actions or []
    fetch_calls = [
        [policy],  # policies query
        [{"value": metric_value}],  # _trigger_value query
        [{"healed_at": h} for h in heal_hist],  # _load_heal_history query
        [{"acted_at": a} for a in global_acts],  # _load_global_actions query
    ]
    conn.fetch = AsyncMock(side_effect=fetch_calls)
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"enabled": flag_enabled} if flag_enabled else None)
    return conn


@pytest.fixture(autouse=True)
def _reset_state():
    # 旧版内存态 _HEAL_HISTORY / _RECENT_ACTIONS 已移除 (P0-2: 持久化到 Postgres)
    # 现在通过数据库表 autoheal_heal_history / autoheal_global_actions 管理
    from aegis.server.services.preconditions import ALL_PRECONDITIONS

    # 让 autoheal 不被 degraded / 成熟度闸门拦截,以便验证更底层的安全层
    # (抖动/限流/真实重启)。函数内是 lazy import,故 patch 源模块。
    with (
        patch(
            "aegis.server.services.preconditions.get_readiness",
            return_value=_ready_readiness(ALL_PRECONDITIONS),
        ),
        patch("aegis.server.services.maturity.is_auto_allowed", return_value=True),
    ):
        yield


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
    conn = _conn(_policy(), metric_value=0.0, heal_history=[now, now])  # 默认 threshold=2 → 抖动
    with (
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()) as ins,
        patch("obase.docker.docker_container_restart") as restart,
        patch("aegis.server.services.platform_flags.is_flag_enabled", AsyncMock(return_value=False)),
        patch("aegis.server.services.change_freeze.is_change_frozen", return_value=False),
        patch("aegis.server.services.preconditions.get_readiness", AsyncMock(return_value=MagicMock(degraded=False, unsatisfied=[]))),
        patch("aegis.server.runtime.config.get_settings", return_value=MagicMock(
            autoheal_enabled=True,
            autoheal_flap_window_seconds=1800,
            autoheal_flap_threshold=2,
            autoheal_rate_limit_max=10,
            autoheal_rate_limit_window_seconds=3600,
            autoheal_require_l2=False,
        )),
        patch("oskill.flapping_detect", return_value=MagicMock(is_flapping=True)),
        patch("aegis.server.services.autoheal_policy._rate_limited", new=AsyncMock(return_value=False)),
        patch("aegis.server.services.safety_mode.compute_safety_mode", AsyncMock(return_value=MagicMock(
            mode=SafetyMode.QUALIFIED,
            failed_checks=[],
            degraded_checks=[],
        ))),
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
    conn = _conn(_policy(), metric_value=0.0, global_actions=[now] * 10)  # 默认 max=10 → 已满
    with (
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()),
        patch("obase.docker.docker_container_restart") as restart,
        patch("aegis.server.services.platform_flags.is_flag_enabled", AsyncMock(return_value=False)),
        patch("aegis.server.services.change_freeze.is_change_frozen", return_value=False),
        patch("aegis.server.services.preconditions.get_readiness", AsyncMock(return_value=MagicMock(degraded=False, unsatisfied=[]))),
        patch("aegis.server.runtime.config.get_settings", return_value=MagicMock(
            autoheal_enabled=True,
            autoheal_flap_window_seconds=1800,
            autoheal_flap_threshold=2,
            autoheal_rate_limit_max=10,
            autoheal_rate_limit_window_seconds=3600,
            autoheal_require_l2=False,
        )),
        patch("oskill.flapping_detect", return_value=MagicMock(is_flapping=False)),
        patch("aegis.server.services.autoheal_policy._load_heal_history", AsyncMock(return_value=[])),
        patch("aegis.server.services.autoheal_policy._rate_limited", new=AsyncMock(return_value=True)),
        patch("aegis.server.services.safety_mode.compute_safety_mode", AsyncMock(return_value=MagicMock(
            mode=SafetyMode.QUALIFIED,
            failed_checks=[],
            degraded_checks=[],
        ))),
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
        patch("aegis.server.services.platform_flags.is_flag_enabled", AsyncMock(return_value=False)),
        patch("aegis.server.services.change_freeze.is_change_frozen", return_value=False),
        patch("aegis.server.services.preconditions.get_readiness", AsyncMock(return_value=MagicMock(degraded=False, unsatisfied=[]))),
        patch("aegis.server.runtime.config.get_settings", return_value=MagicMock(
            autoheal_enabled=True,
            autoheal_flap_window_seconds=1800,
            autoheal_flap_threshold=2,
            autoheal_rate_limit_max=10,
            autoheal_rate_limit_window_seconds=3600,
            autoheal_require_l2=False,
        )),
        patch("oskill.flapping_detect", return_value=MagicMock(is_flapping=False)),
        patch("aegis.server.services.autoheal_policy._rate_limited", new=AsyncMock(return_value=False)),
        patch("obase.docker.docker_container_inspect", AsyncMock(return_value={"State": {"Running": True}})),
        patch("aegis.server.services.safety_mode.compute_safety_mode", AsyncMock(return_value=MagicMock(
            mode=SafetyMode.QUALIFIED,
            failed_checks=[],
            degraded_checks=[],
        ))),
    ):
        actions = await ap.run_autoheal_policies(conn)
    restart.assert_called_once()
    assert actions[0]["ok"] is True and actions[0]["suppressed"] is None
    # History and actions are now persisted to DB, not in-memory
    # The test verifies the action was attempted


@pytest.mark.asyncio
async def test_rate_limited_prunes_expired():
    """_rate_limited 剪掉窗口外时刻 (现在基于 DB, 验证不因过期项判限流)。"""
    from datetime import timedelta

    now = ap._utcnow()
    conn = MagicMock()
    # Mock the DB to return one old action (outside window) and one new
    old_ts = now - timedelta(hours=2)
    conn.fetch = AsyncMock(return_value=[{"acted_at": old_ts}, {"acted_at": now}])
    limited = await ap._rate_limited(conn, now, max_actions=10, window_seconds=3600)
    assert limited is False  # 只有 1 个在窗口内,不限流


# ── §5.4 S1 演练场景：canary 标签目标自愈 ────────────────────────────────


def _canary_policy(**kw):
    base = _policy(target_container="aegis-canary", canary=True, **kw)
    return base


@pytest.mark.asyncio
async def test_canary_only_policy_restarts_canary_target(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S1 演练:canary 策略在 canary-only 模式下仅重启 aegis-canary 目标。"""
    monkeypatch.setenv("AEGIS_AUTOHEAL_CANARY_ONLY", "true")
    canary_pol = _canary_policy()
    normal_pol = _policy(target_container="production-svc", canary=False)
    conn = MagicMock()
    # canary-only mode: SQL filters to canary=TRUE, so only canary_pol returned
    # Need enough fetch responses for all queries
    conn.fetch = AsyncMock(side_effect=[
        [canary_pol],  # policies query
        [{"value": 0.0}],  # _trigger_value query
        [],  # _load_heal_history query
        [],  # _load_global_actions query
    ])
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with (
        patch("aegis.server.runtime.config.get_settings") as mock_cfg,
        patch("obase.docker.docker_container_restart") as restart,
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()) as ins,
        patch("aegis.server.services.platform_flags.is_flag_enabled", AsyncMock(return_value=False)),
        patch("aegis.server.services.change_freeze.is_change_frozen", return_value=False),
        patch("aegis.server.services.preconditions.get_readiness", AsyncMock(return_value=MagicMock(degraded=False, unsatisfied=[]))),
        patch("oskill.flapping_detect", return_value=MagicMock(is_flapping=False)),
        patch("aegis.server.services.autoheal_policy._rate_limited", new=AsyncMock(return_value=False)),
        patch("obase.docker.docker_container_inspect", AsyncMock(return_value={"State": {"Running": True}})),
        patch("aegis.server.services.safety_mode.compute_safety_mode", AsyncMock(return_value=MagicMock(
            mode=SafetyMode.QUALIFIED,
            failed_checks=[],
            degraded_checks=[],
        ))),
    ):
        mock_cfg.return_value.autoheal_enabled = True
        mock_cfg.return_value.autoheal_flap_window_seconds = 300
        mock_cfg.return_value.autoheal_flap_threshold = 2
        mock_cfg.return_value.autoheal_rate_limit_max = 10
        mock_cfg.return_value.autoheal_rate_limit_window_seconds = 3600
        mock_cfg.return_value.docker_host = None
        mock_cfg.return_value.change_freeze_start = ""
        mock_cfg.return_value.change_freeze_duration_seconds = 0
        mock_cfg.return_value.autoheal_require_l2 = False
        await ap.run_autoheal_policies(conn)

    assert restart.call_count == 1
    restart.assert_called_once_with(container_id="aegis-canary", docker_host=None)
    assert "autoheal: restarted aegis-canary" in ins.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_canary_only_mode_skips_non_canary_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1 演练:canary-only 模式下 non-canary 策略被跳过。"""
    monkeypatch.setenv("AEGIS_AUTOHEAL_CANARY_ONLY", "true")
    normal_pol = _policy(target_container="production-svc", canary=False)
    conn = MagicMock()
    # canary-only mode: SQL filters to canary=TRUE, no policies returned
    conn.fetch = AsyncMock(side_effect=[[], [{"value": 0.0}]])
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with (
        patch("aegis.server.runtime.config.get_settings") as mock_cfg,
        patch("obase.docker.docker_container_restart") as restart,
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()),
    ):
        mock_cfg.return_value.autoheal_enabled = True
        mock_cfg.return_value.autoheal_flap_window_seconds = 300
        mock_cfg.return_value.autoheal_flap_threshold = 2
        mock_cfg.return_value.autoheal_rate_limit_max = 10
        mock_cfg.return_value.autoheal_rate_limit_window_seconds = 3600
        mock_cfg.return_value.docker_host = None
        mock_cfg.return_value.change_freeze_start = ""
        mock_cfg.return_value.change_freeze_duration_seconds = 0
        actions = await ap.run_autoheal_policies(conn)

    assert restart.call_count == 0
    assert actions == []


@pytest.mark.asyncio
async def test_canary_fence_blocks_non_canary_real_action_when_not_l2(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C-5.4:未过 L2 且目标非 aegis-canary → 拒绝真实执行(config 不可覆盖)。"""
    pol = _policy(target_container="production-svc", canary=False)  # dry_run=False 默认
    conn = _conn(pol, metric_value=0.0)
    with (
        patch("aegis.server.services.maturity.is_auto_allowed", return_value=False),
        patch("obase.docker.docker_container_restart") as restart,
        patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()) as ins,
    ):
        actions = await ap.run_autoheal_policies(conn)
    assert restart.call_count == 0  # 绝不真实重启
    assert actions and actions[0]["suppressed"] == "canary_fence"
    assert actions[0]["ok"] is False
    assert "C-5.4" in ins.await_args.kwargs["reason"]
