"""Tests for aegis.server.autoheal.engine — AutoHealEngine 4-phase lifecycle."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aegis_autoheal_sdk import ActionResultStatus

from aegis.server.autoheal.engine import AutoHealEngine, AutoHealState

# ── helpers ────────────────────────────────────────────────────────────────────


def _matched_result(confidence: float = 0.9) -> MagicMock:
    r = MagicMock()
    r.matched = True
    r.pattern_name = "high_cpu"
    r.confidence = confidence
    return r


def _no_match_result() -> MagicMock:
    r = MagicMock()
    r.matched = False
    r.pattern_name = None
    r.confidence = 0.0
    return r


def _cb_clear() -> MagicMock:
    cb = MagicMock()
    cb.should_trip = False
    cb.reasons = []
    return cb


def _cb_tripped() -> MagicMock:
    cb = MagicMock()
    cb.should_trip = True
    cb.reasons = ["error_rate too high"]
    return cb


def _mock_plugin(success=True):
    m = MagicMock()
    m.pre_check = AsyncMock(return_value=True)
    m.execute = AsyncMock(return_value=MagicMock(status=ActionResultStatus.OK, detail="ok"))
    m.post_verify = AsyncMock(return_value=success)
    m.rollback = AsyncMock()
    return m


_SIGNAL = {"alert_name": "disk_full", "severity": "critical"}
_PLAN = {"action_kind": "clear_logs", "requires_approval": False}


# ── AutoHealState enum ─────────────────────────────────────────────────────────


def test_autoheal_state_values() -> None:
    assert AutoHealState.pending == "pending"
    assert AutoHealState.failed == "failed"
    assert AutoHealState.succeeded == "succeeded"
    assert AutoHealState.rolled_back == "rolled_back"


# ── Phase 1: diagnosing → failed (no match) ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_fails_when_no_pattern_match() -> None:
    engine = AutoHealEngine()
    with patch(
        "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_no_match_result()
    ):
        state = await engine.run(signal=_SIGNAL, action_plan=_PLAN)
    assert state == AutoHealState.failed
    assert engine.state == AutoHealState.failed


@pytest.mark.asyncio
async def test_run_records_diagnose_event_in_history() -> None:
    engine = AutoHealEngine()
    with patch(
        "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_no_match_result()
    ):
        await engine.run(signal=_SIGNAL, action_plan=_PLAN)
    events = [e["event"] for e in engine.history]
    assert "diagnose_result" in events


# ── Circuit breaker guard ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_fails_when_circuit_breaker_trips() -> None:
    engine = AutoHealEngine()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.circuit_breaker_check", return_value=_cb_tripped()),
    ):
        state = await engine.run(signal=_SIGNAL, action_plan=_PLAN, health_samples=[{"x": 1}])
    assert state == AutoHealState.failed


@pytest.mark.asyncio
async def test_run_skips_circuit_breaker_when_no_samples() -> None:
    """No health_samples → circuit_breaker_check must NOT be called."""
    engine = AutoHealEngine()
    plugin_mock = _mock_plugin()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.circuit_breaker_check") as mock_cb,
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
    ):
        await engine.run(signal=_SIGNAL, action_plan=_PLAN)
    mock_cb.assert_not_called()


# ── Phase 2 → Phase 3: applying → verifying → succeeded ──────────────────────


@pytest.mark.asyncio
async def test_run_succeeds_when_plugin_verifies() -> None:
    engine = AutoHealEngine()
    plugin_mock = _mock_plugin(success=True)
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
    ):
        state = await engine.run(signal=_SIGNAL, action_plan=_PLAN, plugin_name="test-plugin")
    assert state == AutoHealState.succeeded
    plugin_mock.pre_check.assert_called_once()
    plugin_mock.execute.assert_called_once()
    plugin_mock.post_verify.assert_called_once()


@pytest.mark.asyncio
async def test_run_skips_applying_if_pre_check_false() -> None:
    engine = AutoHealEngine()
    plugin_mock = _mock_plugin()
    plugin_mock.pre_check = AsyncMock(return_value=False)
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
    ):
        state = await engine.run(signal=_SIGNAL, action_plan=_PLAN)
    assert state == AutoHealState.succeeded
    plugin_mock.execute.assert_not_called()


# ── Phase 4: rolling_back → rolled_back ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_rolls_back_when_plugin_verify_fails() -> None:
    engine = AutoHealEngine()
    plugin_mock = _mock_plugin(success=False)
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
    ):
        state = await engine.run(signal=_SIGNAL, action_plan=_PLAN)
    assert state == AutoHealState.rolled_back
    plugin_mock.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_rollback_event_recorded_in_history() -> None:
    engine = AutoHealEngine()
    plugin_mock = _mock_plugin(success=False)
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
    ):
        await engine.run(signal=_SIGNAL, action_plan=_PLAN)
    events = [e["event"] for e in engine.history]
    assert "triggering_plugin_rollback" in events
    assert "rollback_start" in events
    assert "rolled_back" in events


# ── C2-3 approval gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_skips_approval_when_no_release_gate_service() -> None:
    engine = AutoHealEngine(release_gate_service=None)
    plugin_mock = _mock_plugin()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
    ):
        state = await engine.run(
            signal=_SIGNAL,
            action_plan={**_PLAN, "requires_approval": True},
            org_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
        )
    assert state == AutoHealState.succeeded


@pytest.mark.asyncio
async def test_run_skips_approval_when_requires_approval_false() -> None:
    mock_rgs = MagicMock()
    mock_rgs.create_gate = AsyncMock()
    engine = AutoHealEngine(release_gate_service=mock_rgs)
    plugin_mock = _mock_plugin()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
    ):
        state = await engine.run(
            signal=_SIGNAL,
            action_plan={**_PLAN, "requires_approval": False},
            org_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
        )
    mock_rgs.create_gate.assert_not_called()
    assert state == AutoHealState.succeeded


@pytest.mark.asyncio
async def test_run_approval_rejected_returns_failed() -> None:
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    gate_id = uuid.uuid4()

    gate_mock = MagicMock()
    gate_mock.gate_id = gate_id

    decided_rejected = MagicMock()
    decided_rejected.state = "rejected"

    mock_rgs = MagicMock()
    mock_rgs.create_gate = AsyncMock(return_value=gate_mock)
    mock_rgs.repo = MagicMock()
    mock_rgs.repo.get = AsyncMock(return_value=decided_rejected)

    engine = AutoHealEngine(release_gate_service=mock_rgs)
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        state = await engine.run(
            signal=_SIGNAL,
            action_plan={**_PLAN, "requires_approval": True},
            org_id=org_id,
            project_id=project_id,
        )
    assert state == AutoHealState.failed


@pytest.mark.asyncio
async def test_run_approval_granted_continues_to_succeeded() -> None:
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    gate_id = uuid.uuid4()

    gate_mock = MagicMock()
    gate_mock.gate_id = gate_id

    # _wait_for_decision: first call returns pending (loop once), second returns approved
    decided_pending = MagicMock()
    decided_pending.state = "pending"
    decided_approved = MagicMock()
    decided_approved.state = "approved"

    mock_rgs = MagicMock()
    mock_rgs.create_gate = AsyncMock(return_value=gate_mock)
    mock_rgs.repo = MagicMock()
    # Sequence: pending (in _wait_for_decision), not-pending (exit loop), approved (in run)
    mock_rgs.repo.get = AsyncMock(side_effect=[decided_pending, decided_approved, decided_approved])

    engine = AutoHealEngine(release_gate_service=mock_rgs)
    plugin_mock = _mock_plugin()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        state = await engine.run(
            signal=_SIGNAL,
            action_plan={**_PLAN, "requires_approval": True},
            org_id=org_id,
            project_id=project_id,
        )
    assert state == AutoHealState.succeeded


# ── history recording ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_contains_state_transitions_for_success_path() -> None:
    engine = AutoHealEngine()
    plugin_mock = _mock_plugin()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch(
            "aegis.server.autoheal.engine.get_plugin_callable",
            return_value=MagicMock(return_value=plugin_mock),
        ),
    ):
        await engine.run(signal=_SIGNAL, action_plan=_PLAN)
    states_seen = {e["state"] for e in engine.history}
    assert AutoHealState.diagnosing in states_seen
    assert AutoHealState.applying in states_seen
    assert AutoHealState.verifying in states_seen
    assert AutoHealState.succeeded in states_seen


# ── initial state ─────────────────────────────────────────────────────────────


def test_initial_state_is_pending() -> None:
    engine = AutoHealEngine()
    assert engine.state == AutoHealState.pending
    assert engine.history == []
