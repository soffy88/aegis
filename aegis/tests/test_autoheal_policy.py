"""Tests for policy-driven closed-loop autoheal."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.services import autoheal_policy as ap


def _policy(**kw):
    base = dict(
        id=uuid.uuid4(), org_id=uuid.uuid4(), name="svc-restart",
        target_container="test-svc", trigger_metric="probe_up",
        trigger_operator="<", trigger_threshold=1.0, action="restart",
        dry_run=True, cooldown_seconds=300, docker_host=None, last_triggered_at=None,
    )
    base.update(kw)
    return base


def _conn(policy, metric_value):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[
        [policy],                         # policies query
        [{"value": metric_value}],        # _trigger_value query
    ])
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_dry_run_logs_does_not_restart():
    conn = _conn(_policy(dry_run=True), metric_value=0.0)  # down (<1)
    with patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()) as ins, \
         patch("obase.docker.docker_container_restart") as restart:
        actions = await ap.run_autoheal_policies(conn)
    assert restart.call_count == 0                 # NEVER restarts in dry-run
    assert actions and actions[0]["dry_run"] is True
    assert "DRY-RUN" in ins.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_real_restart_when_breached_and_not_dry_run():
    conn = _conn(_policy(dry_run=False), metric_value=0.0)  # down
    with patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()), \
         patch("obase.docker.docker_container_restart") as restart:
        actions = await ap.run_autoheal_policies(conn)
    restart.assert_called_once()
    assert restart.call_args.kwargs["container_id"] == "test-svc"
    assert actions[0]["ok"] is True and actions[0]["dry_run"] is False


@pytest.mark.asyncio
async def test_no_action_when_not_breached():
    conn = _conn(_policy(dry_run=False), metric_value=1.0)  # up (not <1)
    with patch.object(ap.AutoHealEventRepository, "insert", AsyncMock()) as ins, \
         patch("obase.docker.docker_container_restart") as restart:
        actions = await ap.run_autoheal_policies(conn)
    assert restart.call_count == 0 and not actions
    ins.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_registered_in_cron():
    from aegis.server.orchestration import cron
    scheduled = []

    async def _fake_gather(*coros, **_kw):
        for c in coros:
            scheduled.append(getattr(c, "__name__", str(c)))
            c.close()

    with patch.object(cron.asyncio, "gather", side_effect=_fake_gather), patch.object(
        cron, "_acquire_loop_runner_role", AsyncMock(return_value=AsyncMock())
    ):
        await cron._cron_main(alerter=None)
    assert "_autoheal_policy_loop" in scheduled
