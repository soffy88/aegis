"""Tests for aegis.server.brain.triage — S1 stub validation."""

from __future__ import annotations

import pytest

from aegis.server.brain.triage import triage_signal


@pytest.mark.asyncio
async def test_triage_signal_stub_always_escalates() -> None:
    result = await triage_signal({"signal_id": "sig_001", "severity": "critical"})
    assert result["should_escalate"] is True


@pytest.mark.asyncio
async def test_triage_signal_stub_preserves_severity() -> None:
    result = await triage_signal({"signal_id": "sig_002", "severity": "warning"})
    assert result["severity"] == "warning"


@pytest.mark.asyncio
async def test_triage_signal_stub_defaults_severity_to_medium() -> None:
    result = await triage_signal({"signal_id": "sig_003"})
    assert result["severity"] == "medium"


@pytest.mark.asyncio
async def test_triage_signal_stub_zero_cost() -> None:
    result = await triage_signal({"signal_id": "sig_004", "severity": "high"})
    assert result["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_triage_signal_stub_reason_mentions_version() -> None:
    result = await triage_signal({"signal_id": "sig_005"})
    assert "v0.4.2" in result["reason"]
