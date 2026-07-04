"""Tests for the loop-runner advisory lock (DESIGN §4.1 / C-4.1).

只有拿到 PG advisory 角色锁的实例才跑编排循环 —— 结构性取缔"单 worker"纪律。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.orchestration import cron


def _patch_env(lock_granted: bool):
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=lock_granted)
    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock()
    fake_persistence = MagicMock()
    fake_persistence.get_pool = lambda: pool
    plan = MagicMock()
    plan.key = 123456789
    ctx = patch.dict("sys.modules", {"aegis.server.persistence": fake_persistence})
    plan_patch = patch("oprim.pg_advisory_lock_plan", return_value=plan)
    return conn, pool, plan, ctx, plan_patch


@pytest.mark.asyncio
async def test_acquires_role_when_lock_granted():
    conn, pool, plan, ctx, plan_patch = _patch_env(lock_granted=True)
    with ctx, plan_patch:
        result = await cron._acquire_loop_runner_role()
    assert result is conn  # 赢得角色 → 返回持有的连接
    # 用派生 key 调 pg_try_advisory_lock
    conn.fetchval.assert_awaited_once_with("SELECT pg_try_advisory_lock($1)", plan.key)
    pool.release.assert_not_called()  # 连接被持有,不归还


@pytest.mark.asyncio
async def test_no_role_when_lock_denied():
    conn, pool, plan, ctx, plan_patch = _patch_env(lock_granted=False)
    with ctx, plan_patch:
        result = await cron._acquire_loop_runner_role()
    assert result is None  # 未拿到 → API-only
    pool.release.assert_awaited_once_with(conn)  # 连接归还池


@pytest.mark.asyncio
async def test_lock_error_returns_none_and_releases():
    conn, pool, plan, ctx, plan_patch = _patch_env(lock_granted=True)
    conn.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    with ctx, plan_patch:
        result = await cron._acquire_loop_runner_role()
    assert result is None  # 出错不阻断,降级 API-only
    pool.release.assert_awaited_once_with(conn)


@pytest.mark.asyncio
async def test_cron_main_skips_loops_when_role_not_acquired():
    # 未拿到角色 → _cron_main 直接返回,不启动任何循环
    with patch.object(cron, "_acquire_loop_runner_role", AsyncMock(return_value=None)):
        with patch.object(cron, "_correlator_loop") as m_loop:
            await cron._cron_main(alerter=None)
        m_loop.assert_not_called()
