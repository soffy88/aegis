"""Tests for aegis.server.brain.triage — S1.5 真装配 (TriageEngine on_signal)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.brain.triage import (
    _make_triage_llm_caller,
    _parse_triage_response,
    build_triage_service,
    get_triage_service,
    init_triage_service,
    triage_signal,
)
from aegis.server.runtime.config import AegisSettings


def _cfg(**kwargs: Any) -> AegisSettings:
    return AegisSettings(**kwargs)  # type: ignore[call-arg]


# ── Case 1: on_signal trigger — build + run() sets _ready ─────────────────────


def test_build_triage_service_ready_after_run() -> None:
    """TriageEngine with on_signal trigger: run() is non-blocking, _ready=True."""
    svc = build_triage_service(_cfg())
    assert svc._ready is True


def test_build_triage_service_has_on_signal_trigger() -> None:
    svc = build_triage_service(_cfg())
    assert "on_signal" in svc.trigger


def test_build_triage_service_name() -> None:
    svc = build_triage_service(_cfg())
    assert svc.name == "aegis-triage"


# ── Case 2: injection fields — llm_caller injected, filters empty ─────────────


def test_build_triage_service_llm_caller_injected() -> None:
    svc = build_triage_service(_cfg())
    # TriageEngine stores injected dependencies as a list (inject={"llm_caller": [fn]}).
    assert isinstance(svc.llm_caller, list)
    assert callable(svc.llm_caller[0])


def test_build_triage_service_filters_empty() -> None:
    """No layer4 filters wired in Aegis — filters list must be empty."""
    svc = build_triage_service(_cfg())
    assert svc.filters == []


def test_build_triage_service_config_has_llm_config() -> None:
    svc = build_triage_service(_cfg())
    assert "llm_config" in svc.config
    assert "model" in svc.config["llm_config"]
    assert "max_tokens" in svc.config["llm_config"]


def test_init_triage_service_sets_singleton() -> None:
    svc = init_triage_service(_cfg())
    assert get_triage_service() is svc


# ── Case 3: process API — triage_signal calls svc.process ─────────────────────


@pytest.mark.asyncio
async def test_triage_signal_calls_process_and_returns_dict() -> None:
    """triage_signal() delegates to svc.process() and returns its result."""
    scored = {
        "priority_score": 80,
        "should_escalate": True,
        "reason": "high_cpu",
        "signal_id": "s1",
    }
    mock_svc = MagicMock()
    mock_svc.process = AsyncMock(return_value=scored)

    with patch("aegis.server.brain.triage._triage_service", mock_svc):
        result = await triage_signal({"signal_id": "s1", "severity": "critical"})

    mock_svc.process.assert_called_once()
    assert result["priority_score"] == 80
    assert result["should_escalate"] is True


@pytest.mark.asyncio
async def test_triage_signal_returns_fallback_when_service_none() -> None:
    """When service not initialized, returns safe fallback (should_escalate=True)."""
    with patch("aegis.server.brain.triage._triage_service", None):
        result = await triage_signal({"signal_id": "s2", "severity": "high"})
    assert result["should_escalate"] is True
    assert "priority_score" in result


@pytest.mark.asyncio
async def test_triage_signal_filtered_out_returns_no_escalate() -> None:
    """process() returns None (all filters rejected) → should_escalate=False."""
    mock_svc = MagicMock()
    mock_svc.process = AsyncMock(return_value=None)

    with patch("aegis.server.brain.triage._triage_service", mock_svc):
        result = await triage_signal({"signal_id": "s3", "severity": "low"})
    assert result["should_escalate"] is False
    assert result["priority_score"] == 0


# ── Case 4: cost path — llm_caller bridge parses priority_score ───────────────


def test_make_triage_llm_caller_returns_scored_events() -> None:
    """llm_caller bridge calls base_caller and returns list with priority_score."""
    mock_resp = {
        "content": [
            {
                "type": "text",
                "text": (
                    '[{"priority_score": 90, "should_escalate": true,'
                    ' "reason": "critical", "signal_id": "x"}]'
                ),
            }
        ]
    }
    mock_base = MagicMock(return_value=mock_resp)
    caller = _make_triage_llm_caller(mock_base, "claude-haiku-4-5", 1024)

    events = [{"signal_id": "x", "severity": "critical"}]
    result = caller(config={}, events=events, mode="score")

    mock_base.assert_called_once()
    assert len(result) == 1
    assert result[0]["priority_score"] == 90
    assert result[0]["should_escalate"] is True


def test_make_triage_llm_caller_fallback_when_no_provider() -> None:
    """No provider registered → fallback with priority_score=50."""
    caller = _make_triage_llm_caller(None, "claude-haiku-4-5", 1024)
    events = [{"signal_id": "y", "severity": "warning"}]
    result = caller(config={}, events=events, mode="score")
    assert result[0]["priority_score"] == 50
    assert result[0]["should_escalate"] is True


def test_parse_triage_response_valid_json() -> None:
    text = '[{"priority_score": 70, "should_escalate": true, "reason": "ok"}]'
    events = [{"signal_id": "z"}]
    result = _parse_triage_response(text, events)
    assert result[0]["priority_score"] == 70


def test_parse_triage_response_strips_fences() -> None:
    text = '```json\n[{"priority_score": 55}]\n```'
    result = _parse_triage_response(text, [{}])
    assert result[0]["priority_score"] == 55


def test_parse_triage_response_fallback_on_invalid() -> None:
    result = _parse_triage_response("not json", [{"signal_id": "q"}])
    assert result[0]["priority_score"] == 50
    assert "signal_id" in result[0]
