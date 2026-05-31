"""Tests for C2-3: AutoHeal approval gate integration.

Tests the approval branch added to AutoHealEngine.handle():
- Plugin with requires_approval=True → awaiting_approval → gate created
- approved → continues to executing → completed
- rejected / expired → cancelled
- pre_check failure → skips approval entirely
- Plugin without requires_approval (legacy) → runs directly
- _wait_for_decision polling behaviour
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aegis.server.orchestration.autoheal import AutoHealEngine, EngineState, PluginResult
from aegis.server.orchestration.plugins.destructive_action_plugin import DestructiveActionPlugin

os.environ.setdefault("AEGIS_JWT_SECRET", "test-secret-do-not-use-in-production-abc!")

_ORG = uuid.UUID("aaaa0001-0000-0000-0000-000000000000")
_PROJ = uuid.UUID("aaaa0002-0000-0000-0000-000000000000")
_USER = uuid.UUID("aaaa0003-0000-0000-0000-000000000000")
_GATE_ID = uuid.UUID("aaaa0004-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# Plugin helpers
# ---------------------------------------------------------------------------


@dataclass
class SimplePlugin:
    """Legacy plugin — no requires_approval method."""

    name: str = "simple-plugin"

    async def pre_check(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True)

    async def execute(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True, requires_restart_verify=False)

    async def rollback(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True)


@dataclass
class ApprovalPlugin:
    """Plugin that always requires approval."""

    name: str = "approval-plugin"
    action_kind: str = "approval_action"
    _approval_needed: bool = True

    async def pre_check(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True)

    async def requires_approval(self, context: dict[str, Any]) -> bool:
        return self._approval_needed

    async def execute(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True, requires_restart_verify=False)

    async def rollback(self, context: dict[str, Any]) -> PluginResult:
        return PluginResult(success=True)


def _make_gate(state: str) -> MagicMock:
    gate = MagicMock()
    gate.gate_id = _GATE_ID
    gate.state = state
    return gate


def _make_service(create_state: str = "pending", final_state: str = "approved") -> MagicMock:
    """Build a mock ReleaseGateService."""
    service = MagicMock()
    service.create_gate = AsyncMock(return_value=_make_gate(create_state))
    repo = MagicMock()
    # get() called twice: first in _wait_for_decision (returns non-pending), then for final check
    repo.get = AsyncMock(
        side_effect=[
            _make_gate(final_state),  # _wait_for_decision sees final state immediately
            _make_gate(final_state),  # decide re-fetch
        ]
    )
    service.repo = repo
    return service


# ---------------------------------------------------------------------------
# State machine tests
# ---------------------------------------------------------------------------


class TestNoApprovalRequired:
    async def test_legacy_plugin_runs_directly(self) -> None:
        """Plugin without requires_approval skips the approval gate."""
        engine = AutoHealEngine(release_gate_service=_make_service())
        state = await engine.handle(
            alert={"alert_name": "test"},
            action_plan={},
            plugin=SimplePlugin(),
            org_id=_ORG,
            project_id=_PROJ,
        )
        assert state == EngineState.completed
        assert EngineState.awaiting_approval not in [h["state"] for h in engine.history]

    async def test_approval_plugin_without_service_runs_directly(self) -> None:
        """No release_gate_service injected → always skip approval."""
        engine = AutoHealEngine(release_gate_service=None)
        state = await engine.handle(
            alert={"alert_name": "test"},
            action_plan={},
            plugin=ApprovalPlugin(),
            org_id=_ORG,
            project_id=_PROJ,
        )
        assert state == EngineState.completed

    async def test_approval_plugin_without_org_id_runs_directly(self) -> None:
        """Missing org_id → approval gate skipped even with service."""
        engine = AutoHealEngine(release_gate_service=_make_service())
        state = await engine.handle(
            alert={"alert_name": "test"},
            action_plan={},
            plugin=ApprovalPlugin(),
        )
        assert state == EngineState.completed


class TestApprovalRequired:
    async def test_approval_creates_gate_and_enters_awaiting(self) -> None:
        service = _make_service(final_state="approved")
        engine = AutoHealEngine(release_gate_service=service)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            state = await engine.handle(
                alert={"alert_name": "test"},
                action_plan={},
                plugin=ApprovalPlugin(),
                org_id=_ORG,
                project_id=_PROJ,
                requested_by=_USER,
            )

        service.create_gate.assert_awaited_once()
        call_kwargs = service.create_gate.call_args.kwargs
        assert call_kwargs["org_id"] == _ORG
        assert call_kwargs["action_kind"] == "approval_action"
        awaiting_events = [h for h in engine.history if h["state"] == EngineState.awaiting_approval]
        assert awaiting_events, "engine must enter awaiting_approval state"
        assert state == EngineState.completed

    async def test_approved_continues_to_completed(self) -> None:
        engine = AutoHealEngine(release_gate_service=_make_service(final_state="approved"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            state = await engine.handle(
                alert={},
                action_plan={},
                plugin=ApprovalPlugin(),
                org_id=_ORG,
                project_id=_PROJ,
            )
        assert state == EngineState.completed

    async def test_rejected_returns_cancelled(self) -> None:
        engine = AutoHealEngine(release_gate_service=_make_service(final_state="rejected"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            state = await engine.handle(
                alert={},
                action_plan={},
                plugin=ApprovalPlugin(),
                org_id=_ORG,
                project_id=_PROJ,
            )
        assert state == EngineState.cancelled

    async def test_expired_returns_cancelled(self) -> None:
        engine = AutoHealEngine(release_gate_service=_make_service(final_state="expired"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            state = await engine.handle(
                alert={},
                action_plan={},
                plugin=ApprovalPlugin(),
                org_id=_ORG,
                project_id=_PROJ,
            )
        assert state == EngineState.cancelled

    async def test_pre_check_failed_skips_approval(self) -> None:
        """Pre-check failure → failed immediately, gate is never created."""
        service = _make_service()
        plugin = ApprovalPlugin()
        plugin.pre_check_fails = True

        @dataclass
        class FailingPlugin:
            name: str = "failing"
            action_kind: str = "fail"

            async def pre_check(self, context: dict[str, Any]) -> PluginResult:
                return PluginResult(success=False)

            async def requires_approval(self, context: dict[str, Any]) -> bool:
                return True

            async def execute(self, context: dict[str, Any]) -> PluginResult:
                return PluginResult(success=True)

            async def rollback(self, context: dict[str, Any]) -> PluginResult:
                return PluginResult(success=True)

        engine = AutoHealEngine(release_gate_service=service)
        state = await engine.handle(
            alert={},
            action_plan={},
            plugin=FailingPlugin(),
            org_id=_ORG,
            project_id=_PROJ,
        )
        assert state == EngineState.failed
        service.create_gate.assert_not_awaited()


class TestWaitForDecision:
    async def test_polling_returns_when_decided(self) -> None:
        """_wait_for_decision returns as soon as gate leaves pending state."""
        service = MagicMock()
        pending = _make_gate("pending")
        approved = _make_gate("approved")
        service.repo = MagicMock()
        # First call pending, second call approved
        service.repo.get = AsyncMock(side_effect=[pending, approved])

        engine = AutoHealEngine(release_gate_service=service)

        sleep_calls = []

        async def _fake_sleep(sec: float) -> None:
            sleep_calls.append(sec)

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await engine._wait_for_decision(gate_id=_GATE_ID, org_id=_ORG, poll_interval_sec=1)

        assert len(sleep_calls) == 1, "should sleep exactly once before approved"

    async def test_polling_timeout_exits_without_decision(self) -> None:
        """_wait_for_decision exits after max_wait_sec even if still pending."""
        service = MagicMock()
        service.repo = MagicMock()
        service.repo.get = AsyncMock(return_value=_make_gate("pending"))

        engine = AutoHealEngine(release_gate_service=service)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await engine._wait_for_decision(
                gate_id=_GATE_ID,
                org_id=_ORG,
                poll_interval_sec=10,
                max_wait_sec=10,  # only 1 iteration
            )
        # Should not raise; exits gracefully


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------


class TestDestructiveActionPlugin:
    async def test_requires_approval_always_true(self) -> None:
        plugin = DestructiveActionPlugin()
        result = await plugin.requires_approval({})
        assert result is True

    async def test_pre_check_always_ok(self) -> None:
        plugin = DestructiveActionPlugin()
        result = await plugin.pre_check({})
        assert result.success is True

    async def test_execute_is_noop(self) -> None:
        plugin = DestructiveActionPlugin()
        result = await plugin.execute({})
        assert result.success is True
        assert result.requires_restart_verify is False

    async def test_action_kind_attribute(self) -> None:
        plugin = DestructiveActionPlugin()
        assert plugin.action_kind == "destructive_action_demo"
        assert plugin.name == "destructive_action_demo"


class TestDefaultRequiresApproval:
    async def test_simple_plugin_has_no_requires_approval(self) -> None:
        """Legacy plugins without requires_approval are handled safely."""
        plugin = SimplePlugin()
        fn = getattr(plugin, "requires_approval", None)
        assert fn is None, "SimplePlugin should not have requires_approval"

    async def test_approval_plugin_requires_approval_true(self) -> None:
        plugin = ApprovalPlugin()
        assert await plugin.requires_approval({}) is True
