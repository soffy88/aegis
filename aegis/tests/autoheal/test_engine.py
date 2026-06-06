"""Tests for aegis.server.autoheal.engine — AutoHealEngine 4-phase lifecycle."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.circuit_breaker_check") as mock_cb,
        patch("aegis.server.autoheal.engine.verify_health_after_action", return_value=True),
    ):
        await engine.run(signal=_SIGNAL, action_plan=_PLAN)
    mock_cb.assert_not_called()


# ── Phase 2 → Phase 3: applying → verifying → succeeded ──────────────────────


@pytest.mark.asyncio
async def test_run_succeeds_when_health_check_passes() -> None:
    engine = AutoHealEngine()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.verify_health_after_action", return_value=True),
    ):
        state = await engine.run(signal=_SIGNAL, action_plan=_PLAN, service_url="http://svc/health")
    assert state == AutoHealState.succeeded


@pytest.mark.asyncio
async def test_run_succeeds_when_no_service_url() -> None:
    """Empty service_url → skip health check, treat as healthy."""
    engine = AutoHealEngine()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.verify_health_after_action") as mock_health,
    ):
        state = await engine.run(signal=_SIGNAL, action_plan=_PLAN, service_url="")
    mock_health.assert_not_called()
    assert state == AutoHealState.succeeded


# ── Phase 4: rolling_back → rolled_back ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_rolls_back_when_health_check_fails() -> None:
    engine = AutoHealEngine()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.verify_health_after_action", return_value=False),
    ):
        state = await engine.run(signal=_SIGNAL, action_plan=_PLAN, service_url="http://svc/health")
    assert state == AutoHealState.rolled_back


@pytest.mark.asyncio
async def test_rollback_event_recorded_in_history() -> None:
    engine = AutoHealEngine()
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.verify_health_after_action", return_value=False),
    ):
        await engine.run(signal=_SIGNAL, action_plan=_PLAN, service_url="http://svc/health")
    events = [e["event"] for e in engine.history]
    assert "rollback_start" in events
    assert "rolled_back" in events


# ── C2-3 approval gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_skips_approval_when_no_release_gate_service() -> None:
    engine = AutoHealEngine(release_gate_service=None)
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.verify_health_after_action", return_value=True),
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
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.verify_health_after_action", return_value=True),
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
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.verify_health_after_action", return_value=True),
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
    with (
        patch(
            "aegis.server.autoheal.engine.diagnose_pattern_match", return_value=_matched_result()
        ),
        patch("aegis.server.autoheal.engine.verify_health_after_action", return_value=True),
    ):
        await engine.run(signal=_SIGNAL, action_plan=_PLAN, service_url="http://svc/health")
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
