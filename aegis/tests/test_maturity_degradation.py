"""§2.1 / C-2.1: 成熟度阶梯 + 演练结果 + 连续失败自动降级。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aegis.server.services import maturity as mat


def _drill_conn() -> MagicMock:
    """Mock conn:内存维护 capability_maturity 行,允许 record_drill 的多次调用。"""
    state: dict[str, dict] = {}

    async def _execute(q, *args, **kw):
        if "INSERT INTO drill_results" in q:
            return None
        if "capability_maturity" in q:
            # args order: capability, level, consecutive, auto_enabled
            cap, level, consec, auto = args[0], args[1], args[2], args[3]
            state[cap] = {
                "level": level,
                "consecutive_failures": consec,
                "auto_enabled": auto,
            }
            return None
        return None

    async def _fetchrow(q, *args, **kw):
        if "FROM capability_maturity" in q:
            cap = args[0]
            s = state.get(cap)
            if s is None:
                return None
            return {
                "level": s["level"],
                "consecutive_failures": s["consecutive_failures"],
                "auto_enabled": s["auto_enabled"],
                "last_drill_at": None,
                "reason": None,
            }
        return None

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    return conn


@pytest.mark.asyncio
async def test_record_drill_fail_twice_downgrades_to_l1_disables_auto():
    """连续 2 次失败 → 强制降回 L1 且 auto 禁用 (§2.1/C-2.1)。"""
    conn = _drill_conn()
    # 预置一个已 L2 + auto 已启用的状态,代表此前已验证通过
    await conn.execute(
        "INSERT INTO capability_maturity VALUES ($1, $2, $3, $4)",
        "autoheal.restart",
        2,
        0,
        True,
    )
    # 第一次失败:连续 1/2
    r1 = await mat.record_drill(conn, capability="autoheal.restart", scenario="S1", passed=False, threshold=2)
    assert r1["consecutive_failures"] == 1
    assert r1["level"] == 2  # 未达阈值,level 暂保留
    assert r1["auto_enabled"] is True
    # 第二次失败:达阈值 → 强制降级
    r2 = await mat.record_drill(conn, capability="autoheal.restart", scenario="S1", passed=False, threshold=2)
    assert r2["consecutive_failures"] == 2
    assert r2["level"] == 1  # 强制降回
    assert r2["auto_enabled"] is False
    assert r2["downgraded"] is True


@pytest.mark.asyncio
async def test_pass_clears_failure_counter():
    """一次通过 → 清零连续失败计数。"""
    conn = _drill_conn()
    await conn.execute(
        "INSERT INTO capability_maturity VALUES ($1, $2, $3, $4)",
        "autoheal.restart",
        2,
        1,
        True,
    )
    r = await mat.record_drill(conn, capability="autoheal.restart", scenario="S1", passed=True, threshold=2)
    assert r["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_missing_capability_defaults_to_l1_liability():
    """从未演练的能力 → 默认 L1 负债, auto 禁用 (I2)。"""
    conn = _drill_conn()
    st = await mat.capability_state(conn, "backup.restore")
    assert st["level"] == 1
    assert st["auto_enabled"] is False
    assert "负债" in st["reason"] or "never drilled" in st["reason"]


@pytest.mark.asyncio
async def test_is_auto_allowed_respects_level_and_auto_flag():
    """is_auto_allowed 综合 level + auto_enabled + require_l2。"""
    conn = _drill_conn()
    # L1 默认 (缺行) → 即便 require_l2=False 也因 auto_enabled=False 而 False
    assert await mat.is_auto_allowed(conn, "x", require_l2=True) is False
    assert await mat.is_auto_allowed(conn, "x", require_l2=False) is False
    # 升到 L2 + auto=True → 允许
    await conn.execute(
        "INSERT INTO capability_maturity VALUES ($1, $2, $3, $4)",
        "x",
        2,
        0,
        True,
    )
    assert await mat.is_auto_allowed(conn, "x", require_l2=True) is True
    # 但降级后:auto_enabled=False → False
    await conn.execute(
        "INSERT INTO capability_maturity VALUES ($1, $2, $3, $4)",
        "x",
        1,
        2,
        False,
    )
    assert await mat.is_auto_allowed(conn, "x", require_l2=False) is False


@pytest.mark.asyncio
async def test_record_drill_unknown_scenario_rejected():
    """非法演练场景名抛 ValueError。"""
    conn = _drill_conn()
    with pytest.raises(ValueError, match="unknown drill scenario"):
        await mat.record_drill(conn, capability="c", scenario="S9", passed=True)
