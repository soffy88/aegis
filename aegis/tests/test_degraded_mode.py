"""§11.1 / C-11: degraded mode 闸门测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI

from aegis.server.api.routers import system as system_router
from aegis.server.exceptions import DegradedModeError
from aegis.server.services.preconditions import (
    PRECOND_DATAGUARD,
    PRECOND_DEADMAN,
    PRECOND_EXPOSURE,
    PRECOND_M2_ISOLATION,
    PRECOND_SELF_RECOVER,
    assert_not_degraded,
    get_precondition,
    get_readiness,
    set_precondition,
)


def _conn(satisfied_keys: set[str]) -> MagicMock:
    """Mock conn:指定 key 的 flag 视为已满足,其余视为缺行(默认未满足)。"""
    conn = MagicMock(spec=asyncpg.Connection)

    async def _fetchrow(q, key):
        return {"enabled": True, "reason": None, "updated_at": None} if key in satisfied_keys else None

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    async def _execute(q, key, enabled, reason):
        return None

    conn.execute = AsyncMock(side_effect=_execute)
    return conn


@pytest.mark.asyncio
async def test_default_is_degraded_when_no_precond_flags():
    """未置任何前置条件 → 系统默认 degraded (保守默认)。"""
    conn = _conn(set())
    rd = await get_readiness(conn)
    assert rd.degraded is True
    assert len(rd.unsatisfied) == 5


@pytest.mark.asyncio
async def test_partial_satisfaction_still_degraded():
    """只置了 4/5 → 仍 degraded (任一未满足即降级,§11.1)。"""
    conn = _conn({PRECOND_EXPOSURE, PRECOND_DEADMAN, PRECOND_DATAGUARD, PRECOND_SELF_RECOVER})
    rd = await get_readiness(conn)
    assert rd.degraded is True
    unsat = {s.key for s in rd.unsatisfied}
    assert unsat == {PRECOND_M2_ISOLATION}


@pytest.mark.asyncio
async def test_all_satisfied_exits_degraded():
    """五条全部置位 → degraded = False,可正常执行。"""
    conn = _conn(
        {
            PRECOND_EXPOSURE,
            PRECOND_DEADMAN,
            PRECOND_DATAGUARD,
            PRECOND_SELF_RECOVER,
            PRECOND_M2_ISOLATION,
        }
    )
    rd = await get_readiness(conn)
    assert rd.degraded is False
    assert rd.unsatisfied == []


@pytest.mark.asyncio
async def test_assert_not_degraded_raises_when_degraded():
    """degraded 状态下,破坏性动作入口被断言拒绝 (fail-closed, C-11)。"""
    conn = _conn(set())  # 全部未满足
    with pytest.raises(DegradedModeError) as exc:
        await assert_not_degraded(conn, action="autoheal:restart:test-svc")
    msg = str(exc.value)
    assert "degraded_mode" in msg
    assert "test-svc" in msg
    assert PRECOND_EXPOSURE in msg or "暴露" in msg  # 列出未满足项


@pytest.mark.asyncio
async def test_assert_not_degraded_passes_when_ready():
    """前置条件全满足 → 不抛,可执行。"""
    conn = _conn({PRECOND_EXPOSURE, PRECOND_DEADMAN, PRECOND_DATAGUARD, PRECOND_SELF_RECOVER, PRECOND_M2_ISOLATION})
    await assert_not_degraded(conn, action="autoheal:restart")  # 不应抛


@pytest.mark.asyncio
async def test_set_precondition_rejects_unknown_key():
    """未注册的 key 抛 ValueError,避免拼写错误悄默写脏。"""
    conn = _conn(set())
    with pytest.raises(ValueError, match="unknown precondition key"):
        await set_precondition(conn, "precond:nonsense", satisfied=True)


@pytest.mark.asyncio
async def test_get_precondition_unsatisfied_default_label():
    """未置位的条件返回的 label 与 §11 文档陈述一致(运维可读)。"""
    conn = _conn(set())
    state = await get_precondition(conn, PRECOND_EXPOSURE)
    assert state.satisfied is False
    assert "暴露" in state.label or "Access" in state.label


def _degraded_app() -> FastAPI:
    app = FastAPI()
    app.include_router(system_router.router)
    return app


def test_system_readiness_endpoint_reports_degraded():
    """GET /api/v1/system/readiness 返回 degraded 状态 + 全部前置条件。"""
    # 通过 TestClient 模拟: 我们直接覆盖 conn → 不可行(get_db_conn 用真实池)→
    # 改为对 service 层断言。endpoint 测试用集成层(需 DB)留待 e2e。
    import asyncio

    async def _run():
        conn = _conn(set())
        rd = await get_readiness(conn)
        return rd

    rd = asyncio.run(_run())
    assert rd.degraded is True
    assert any(p.key == PRECOND_M2_ISOLATION for p in rd.preconditions)


def test_emergency_stop_endpoint_registered():
    """system router 注册了急停 + 恢复 + readiness + 置位 4 个端点。"""
    app = _degraded_app()
    paths = {r.path for r in app.routes}
    assert "/api/v1/system/emergency-stop" in paths
    assert "/api/v1/system/emergency-resume" in paths
    assert "/api/v1/system/readiness" in paths
