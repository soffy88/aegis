"""Tests for policy-driven closed-loop autoheal."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.services import autoheal_policy as ap
from aegis.server.services.safety_mode import SafetyMode


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
        dry_run=True,
        cooldown_seconds=300,
        docker_host=None,
        canary=False,
        last_triggered_at=None,
    )
    base.update(kw)
    return base


def _conn(policy, metric_value):
    conn = MagicMock()
    # Provide enough fetch responses for all the queries:
    # 1. policies query
    # 2. _trigger_value query
    # 3. _load_heal_history query
    # 4. _load_global_actions query
    # 5. _check_duplicate_execution query (fetchrow)
    # 6. potentially more for post-restart checks
    conn.fetch = AsyncMock(
        side_effect=[
            [policy],  # policies query
            [{"value": metric_value}],  # _trigger_value query
            [],  # _load_heal_history query
            [],  # _load_global_actions query
            [],  # any additional fetch
        ]
    )
    conn.fetchrow = AsyncMock(return_value=None)  # for kill-switch check, duplicate check
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_dry_run_logs_does_not_restart():
    conn = _conn(_policy(dry_run=True), metric_value=0.0)  # down (<1)
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
    ):
        actions = await ap.run_autoheal_policies(conn)
    assert restart.call_count == 0  # NEVER restarts in dry-run
    assert actions and actions[0]["dry_run"] is True
    assert "DRY-RUN" in ins.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_real_restart_when_breached_and_not_dry_run():
    conn = _conn(_policy(dry_run=False), metric_value=0.0)  # down
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
    assert restart.call_args.kwargs["container_id"] == "test-svc"
    assert actions[0]["ok"] is True and actions[0]["dry_run"] is False


@pytest.mark.asyncio
async def test_no_action_when_not_breached():
    conn = _conn(_policy(dry_run=False), metric_value=1.0)  # up (not <1)
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
    ):
        actions = await ap.run_autoheal_policies(conn)
    assert restart.call_count == 0 and not actions
    ins.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_registered_in_cron():
    from aegis.server.orchestration import cron
    from aegis.server.orchestration.loop_supervisor import _supervisor

    scheduled: list[str] = []

    async def _fake_gather(*coros, **_kw):
        for c in coros:
            if hasattr(c, "__name__"):
                scheduled.append(c.__name__)
            elif hasattr(c, "get_name"):
                scheduled.append(c.get_name())
            # Don't call .close() on Task objects - they don't have it
            if hasattr(c, "cancel"):
                c.cancel()

    with (
        patch.object(cron.asyncio, "gather", side_effect=_fake_gather),
        patch.object(cron, "_acquire_loop_runner_role", AsyncMock(return_value=AsyncMock())),
    ):
        await cron._cron_main(alerter=None)

    # Check that the supervisor has the autoheal loop registered
    assert "autoheal" in _supervisor._loops
