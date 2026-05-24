"""Tests for Brain orchestrator (5 tests per §3.3)."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from aegis.server.dispatch.omodul_dispatcher import OmodulDispatcher
from aegis.server.orchestration.brain import run_brain_pipeline


def _mock_dispatcher() -> mock.AsyncMock:
    return mock.AsyncMock(spec=OmodulDispatcher)


def _triage_no_escalate() -> dict[str, Any]:
    findings = mock.MagicMock()
    findings.should_escalate = False
    return {"status": "completed", "findings": findings, "cost_usd": 0.01}


def _triage_escalate() -> dict[str, Any]:
    findings = mock.MagicMock()
    findings.should_escalate = True
    return {"status": "completed", "findings": findings, "cost_usd": 0.01}


def _rca_ok() -> dict[str, Any]:
    findings = mock.MagicMock()
    findings.requires_human = False
    findings.model_dump.return_value = {"root_cause": "disk_full"}
    return {"status": "completed", "findings": findings, "cost_usd": 0.05}


def _rca_requires_human() -> dict[str, Any]:
    findings = mock.MagicMock()
    findings.requires_human = True
    return {"status": "completed", "findings": findings, "cost_usd": 0.05}


def _action_plan_ok() -> dict[str, Any]:
    return {"status": "completed", "findings": {"plan": "restart"}, "cost_usd": 0.03}


@pytest.mark.asyncio
async def test_low_priority_signal_triage_only() -> None:
    """Triage returns should_escalate=False → chain stops at triage_only."""
    dispatcher = _mock_dispatcher()
    dispatcher.invoke.return_value = _triage_no_escalate()

    result = await run_brain_pipeline(
        dispatcher=dispatcher,
        alert_payload={"alert_name": "cpu_idle", "severity": "info"},
        context={},
        user_id="user_1",
    )

    assert result["stage"] == "triage_only"
    assert dispatcher.invoke.call_count == 1


@pytest.mark.asyncio
async def test_high_priority_full_chain() -> None:
    """Triage escalates + RCA ok + Action Plan → full chain completes."""
    dispatcher = _mock_dispatcher()
    dispatcher.invoke.side_effect = [_triage_escalate(), _rca_ok(), _action_plan_ok()]

    result = await run_brain_pipeline(
        dispatcher=dispatcher,
        alert_payload={"alert_name": "disk_full", "severity": "critical"},
        context={"available_tools": ["df", "du"]},
        user_id="user_1",
    )

    assert result["stage"] == "action_plan_ready"
    assert dispatcher.invoke.call_count == 3
    # Verify omodul names called in order
    calls = [c.kwargs["omodul_name"] for c in dispatcher.invoke.call_args_list]
    assert calls == ["triage_signal", "diagnose_root_cause", "propose_action_plan"]


@pytest.mark.asyncio
async def test_rca_requires_human_stops_chain() -> None:
    """RCA requires_human=True → chain stops, no action plan."""
    dispatcher = _mock_dispatcher()
    dispatcher.invoke.side_effect = [_triage_escalate(), _rca_requires_human()]

    result = await run_brain_pipeline(
        dispatcher=dispatcher,
        alert_payload={"alert_name": "x", "severity": "critical"},
        context={},
        user_id="user_1",
    )

    assert result["stage"] == "rca_requires_human"
    assert dispatcher.invoke.call_count == 2


@pytest.mark.asyncio
async def test_brain_does_not_call_omodul_directly() -> None:
    """brain.py only calls dispatcher.invoke, never imports omodul directly."""
    import inspect
    import aegis.server.orchestration.brain as brain_mod

    source = inspect.getsource(brain_mod)
    # Must not have top-level `from omodul import` or `import omodul`
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("from omodul import") or stripped.startswith("import omodul"):
            pytest.fail(f"brain.py directly imports omodul: {stripped}")


@pytest.mark.asyncio
async def test_brain_no_user_id_in_omodul_input() -> None:
    """input_data passed to dispatcher.invoke never contains user_id."""
    dispatcher = _mock_dispatcher()
    dispatcher.invoke.side_effect = [_triage_no_escalate()]

    await run_brain_pipeline(
        dispatcher=dispatcher,
        alert_payload={"alert_name": "test"},
        context={},
        user_id="secret_user_42",
    )

    for call in dispatcher.invoke.call_args_list:
        input_data = call.kwargs.get("input_data", {})
        assert "user_id" not in input_data, "user_id must not leak into omodul input_data"
