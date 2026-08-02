"""Tests for the retention loop + storage guard (DESIGN §7 / I6 / C-I6).

retention 循环按登记表分批删过期遥测(有界写入者),存储守卫在生产盘越阈时大声告警。
删除走本进程 asyncpg 池(`_prune_table`) —— 生产实测 oprim 的 psycopg 驱动未装,
旧实现每轮对每张表都失败、保留策略从未真正生效。
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


def _disk_ok(**_kw):
    return _mk_result(over_threshold=False, used_percent=10.0)


@pytest.mark.asyncio
async def test_retention_loop_prunes_every_registered_table():
    """一轮迭代应对每张登记表调 _prune_table,并执行一次 disk_usage 存储守卫。"""
    pruned: list[dict] = []
    threaded: list[str] = []

    async def fake_to_thread(fn, **kw):
        threaded.append(fn.__name__)
        return _disk_ok()

    async def fake_prune(**kw):
        pruned.append(kw)
        return 0

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
        patch.object(cron, "_prune_table", side_effect=fake_prune),
        pytest.raises(asyncio.CancelledError),
    ):
        await cron._retention_loop()

    assert len(pruned) == len(RETENTION)
    assert {c["table"] for c in pruned} == {str(e["table"]) for e in RETENTION}
    assert {c["ts_column"] for c in pruned} == {str(e["ts_column"]) for e in RETENTION}
    assert "disk_usage" in threaded  # 存储守卫执行


@pytest.mark.asyncio
async def test_storage_guard_breach_logs_warning():
    """disk_usage.over_threshold=True → 记 storage_guard_breach 告警。"""

    async def fake_to_thread(fn, **kw):
        return _mk_result(over_threshold=True, used_percent=88.5)

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
        patch.object(cron, "_prune_table", side_effect=AsyncMock(return_value=0)),
        patch.object(cron.log, "warning") as m_warn,
        pytest.raises(asyncio.CancelledError),
    ):
        await cron._retention_loop()

    assert any("storage_guard_breach" in str(c.args) for c in m_warn.call_args_list)


@pytest.mark.asyncio
async def test_retention_prune_error_does_not_abort_loop():
    """单表 prune 抛错不应阻断后续表或存储守卫。"""
    seen: list[str] = []
    threaded: list[str] = []

    async def fake_to_thread(fn, **kw):
        threaded.append(fn.__name__)
        return _disk_ok()

    async def fake_prune(*, table: str, **kw):
        seen.append(table)
        if table == str(RETENTION[0]["table"]):
            raise RuntimeError("boom")
        return 0

    sleep_mock = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    with (
        patch("asyncio.to_thread", side_effect=fake_to_thread),
        patch("asyncio.sleep", sleep_mock),
        patch.object(cron, "_prune_table", side_effect=fake_prune),
        pytest.raises(asyncio.CancelledError),
    ):
        await cron._retention_loop()

    assert len(seen) == len(RETENTION)
    assert "disk_usage" in threaded


# ── _prune_table ─────────────────────────────────────────────────────────────


def _pool_returning(statuses: list[str]) -> MagicMock:
    """Fake asyncpg pool whose conn.execute returns the given command statuses."""
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=statuses)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


@pytest.mark.asyncio
async def test_prune_table_deletes_in_batches_until_drained():
    """满批说明还有更多待删 → 继续;不满批即停,避免空转。"""
    full = cron._PRUNE_BATCH_ROWS
    pool, conn = _pool_returning([f"DELETE {full}", f"DELETE {full}", "DELETE 7"])
    with patch("aegis.server.persistence.get_pool", return_value=pool):
        deleted = await cron._prune_table(table="agent_metrics", ts_column="ts", retain_days=15.0)
    assert deleted == full * 2 + 7
    assert conn.execute.await_count == 3
    sql = conn.execute.await_args.args[0]
    assert "DELETE FROM agent_metrics" in sql
    assert f"LIMIT {full}" in sql  # 有界删除,不做一把梭


@pytest.mark.asyncio
async def test_prune_table_is_capped_per_tick():
    """即使还有海量待删数据,单轮也有批次上限(不霸占连接池/WAL)。"""
    full = cron._PRUNE_BATCH_ROWS
    pool, conn = _pool_returning([f"DELETE {full}"] * 1000)
    with patch("aegis.server.persistence.get_pool", return_value=pool):
        deleted = await cron._prune_table(table="error_events", ts_column="ts", retain_days=14.0)
    assert conn.execute.await_count == cron._PRUNE_MAX_BATCHES_PER_TABLE
    assert deleted == full * cron._PRUNE_MAX_BATCHES_PER_TABLE


@pytest.mark.asyncio
async def test_prune_table_rejects_unsafe_identifiers():
    """表名/列名直接拼进 SQL,必须先过标识符校验。"""
    with pytest.raises(ValueError, match="unsafe identifier"):
        await cron._prune_table(table="agent_metrics; DROP TABLE x", ts_column="ts", retain_days=1)


@pytest.mark.asyncio
async def test_prune_table_passes_retain_days_as_bound_param():
    pool, conn = _pool_returning(["DELETE 0"])
    with patch("aegis.server.persistence.get_pool", return_value=pool):
        await cron._prune_table(table="audit_log", ts_column="created_at", retain_days=365.0)
    assert conn.execute.await_args.args[1] == "365.0"


def test_retention_registry_is_bounded_and_sane():
    """登记表非空、天数为正、存储阈值合理 —— 结构不变式。"""
    assert RETENTION, "retention 登记表不得为空"
    for e in RETENTION:
        assert int(e["retain_days"]) > 0  # type: ignore[call-overload]
        assert e["table"] and e["ts_column"]
    assert 0 < STORAGE_GUARD_PERCENT < 100
