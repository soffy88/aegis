"""Tests for §9/§3.3 change freeze window."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.services import autoheal_policy as ap
from aegis.server.services.change_freeze import is_change_frozen


def _cfg(**over):
    c = MagicMock()
    c.change_freeze_start = over.get("start", "")
    c.change_freeze_duration_seconds = over.get("duration", 0)
    c.change_freeze_recurrence = over.get("recurrence", "none")
    c.change_freeze_weekdays = over.get("weekdays", "")
    return c


_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def test_disabled_when_unset():
    assert is_change_frozen(_cfg(), _NOW) is False


def test_active_one_shot_window():
    start = _NOW - timedelta(minutes=10)
    cfg = _cfg(start=start.isoformat(), duration=3600, recurrence="none")
    assert is_change_frozen(cfg, _NOW) is True  # now 在 [start, start+1h) 内


def test_inactive_outside_window():
    start = _NOW - timedelta(hours=5)
    cfg = _cfg(start=start.isoformat(), duration=3600, recurrence="none")
    assert is_change_frozen(cfg, _NOW) is False  # 已过窗口


def test_bad_config_fails_open():
    cfg = _cfg(start="not-a-date", duration=3600)
    assert is_change_frozen(cfg, _NOW) is False  # 坏配置 → 不冻结(fail-open)


def test_daily_recurrence_active():
    # 起点昨天同一时刻,daily → 今天该时段仍活跃
    start = _NOW - timedelta(days=1, minutes=5)
    cfg = _cfg(start=start.isoformat(), duration=3600, recurrence="daily")
    assert is_change_frozen(cfg, _NOW) is True


def _policy():
    return dict(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="svc",
        target_container="c",
        trigger_metric="probe_up",
        trigger_operator="<",
        trigger_threshold=1.0,
        action="restart",
        dry_run=False,
        cooldown_seconds=300,
        docker_host=None,
        last_triggered_at=None,
    )


@pytest.mark.asyncio
async def test_autoheal_halts_during_freeze():
    """冻结窗内 run_autoheal_policies 直接返回空,不查策略不动作。"""
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[_policy()], [{"value": 0.0}]])
    conn.fetchrow = AsyncMock(return_value=None)  # kill switch 未置位
    start = ap._utcnow() - timedelta(minutes=1)
    cfg = MagicMock()
    cfg.autoheal_enabled = True
    cfg.change_freeze_start = start.isoformat()
    cfg.change_freeze_duration_seconds = 3600
    cfg.change_freeze_recurrence = "none"
    cfg.change_freeze_weekdays = ""
    with (
        patch("aegis.server.runtime.config.get_settings", return_value=cfg),
        patch("obase.docker.docker_container_restart") as restart,
    ):
        actions = await ap.run_autoheal_policies(conn)
    assert actions == [] and restart.call_count == 0
    conn.fetch.assert_not_awaited()  # 冻结 → 连策略都不查
