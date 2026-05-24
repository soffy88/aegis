"""Tests for C0c-4: autoheal engine → oskill.restart_and_verify."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest

from aegis.server.orchestration.autoheal import AutoHealEngine, EngineState, PluginResult


@dataclass
class FakePlugin:
    """Fake plugin for testing."""

    name: str = "restart-nginx"
    pre_check_result: PluginResult | None = None
    execute_result: PluginResult | None = None

    async def pre_check(self, context: dict[str, Any]) -> PluginResult:
        return self.pre_check_result or PluginResult(success=True)

    async def execute(self, context: dict[str, Any]) -> PluginResult:
        return self.execute_result or PluginResult(
            success=True,
            requires_restart_verify=True,
            container_id="nginx-abc123",
            health_check_url="http://localhost:8080/health",
        )

    async def rollback(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True)


@pytest.mark.asyncio
async def test_engine_calls_oskill_restart_and_verify() -> None:
    """Engine delegates post-verify to oskill.restart_and_verify."""
    outcome = MagicMock()
    outcome.verified_healthy = True

    with mock.patch(
        "aegis.server.orchestration.autoheal.restart_and_verify",
        return_value=outcome,
    ) as m:
        engine = AutoHealEngine()
        state = await engine.handle(
            alert={"alert_name": "nginx_unhealthy"},
            action_plan={"action": "restart"},
            plugin=FakePlugin(),
        )

    m.assert_called_once_with(
        container_id="nginx-abc123",
        health_check_url="http://localhost:8080/health",
        timeout_sec=60,
    )
    assert state == EngineState.completed
    assert engine.outcome == outcome


@pytest.mark.asyncio
async def test_engine_no_plugin_escalates_to_human() -> None:
    """No plugin → escalated_to_human."""
    engine = AutoHealEngine()
    state = await engine.handle(
        alert={"alert_name": "unknown"},
        action_plan={},
        plugin=None,
    )
    assert state == EngineState.escalated_to_human


@pytest.mark.asyncio
async def test_engine_pre_check_fail_stops() -> None:
    """Pre-check failure → failed state, no execute."""
    plugin = FakePlugin(pre_check_result=PluginResult(success=False))

    engine = AutoHealEngine()
    state = await engine.handle(
        alert={"alert_name": "test"},
        action_plan={},
        plugin=plugin,
    )
    assert state == EngineState.failed


@pytest.mark.asyncio
async def test_engine_post_verify_fail_triggers_rollback() -> None:
    """Post-verify unhealthy → rollback → failed."""
    outcome = MagicMock()
    outcome.verified_healthy = False

    with mock.patch(
        "aegis.server.orchestration.autoheal.restart_and_verify",
        return_value=outcome,
    ):
        engine = AutoHealEngine()
        state = await engine.handle(
            alert={"alert_name": "nginx_unhealthy"},
            action_plan={"action": "restart"},
            plugin=FakePlugin(),
        )

    assert state == EngineState.failed
    # Verify rollback was logged
    events = [h["event"] for h in engine.history]
    assert "rollback_start" in events
    assert "rollback_done" in events


def test_engine_does_not_implement_restart_logic_directly() -> None:
    """Static check: autoheal.py imports oskill.restart_and_verify, not docker SDK."""
    import inspect

    import aegis.server.orchestration.autoheal as mod

    source = inspect.getsource(mod)
    assert "from oskill" in source or "import oskill" in source
    assert "restart_and_verify" in source
    # Must NOT contain raw docker restart logic
    assert "docker.from_env" not in source
    assert "container.restart()" not in source
