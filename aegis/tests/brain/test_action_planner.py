"""Tests for aegis.server.brain.action_planner — S1-Planner assembly."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.brain.action_planner import (
    _make_planner_llm_provider,
    _parse_plan_response,
    build_planner_service,
    get_planner_service,
    init_planner_service,
    propose_action_plan,
)
from aegis.server.runtime.config import AegisSettings


def _cfg(**kwargs: Any) -> AegisSettings:
    return AegisSettings(**kwargs)  # type: ignore[arg-type]


# ── build / singleton ─────────────────────────────────────────────────────────


def test_build_planner_service_returns_engine() -> None:
    svc = build_planner_service(_cfg())
    assert hasattr(svc, "run")
    assert hasattr(svc, "health")
    assert hasattr(svc, "submit_request")


def test_init_planner_service_sets_singleton() -> None:
    svc = init_planner_service(_cfg())
    assert get_planner_service() is svc


# ── propose_action_plan ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_propose_action_plan_returns_empty_when_no_service() -> None:
    with patch("aegis.server.brain.action_planner._planner_service", None):
        result = await propose_action_plan({"final_answer": "disk full"})
    assert result == []


@pytest.mark.asyncio
async def test_propose_action_plan_calls_execute_plan() -> None:
    mock_engine = MagicMock()
    mock_engine._execute_plan = AsyncMock(
        return_value={
            "status": "completed",
            "step_results": [{"plugin_id": "free_disk", "status": "ok"}],
        }
    )
    with patch("aegis.server.brain.action_planner._planner_service", mock_engine):
        result = await propose_action_plan({"final_answer": "disk full on /data", "history": []})
    mock_engine._execute_plan.assert_awaited_once()
    request_arg = mock_engine._execute_plan.call_args[0][0]
    assert "disk full" in request_arg["symptom"]
    assert result == [{"plugin_id": "free_disk", "status": "ok"}]


# ── _make_planner_llm_provider ────────────────────────────────────────────────


def test_planner_llm_provider_returns_empty_when_no_caller() -> None:
    provider = _make_planner_llm_provider(None, "any-model")
    result = provider(symptom="disk full", context="")
    assert result == []


def test_planner_llm_provider_calls_base_and_parses_list() -> None:
    steps = [{"plugin_id": "restart", "params": {}, "description": "restart worker"}]
    mock_caller = MagicMock(
        return_value={"content": [{"type": "text", "text": f"{steps}".replace("'", '"')}]}
    )
    provider = _make_planner_llm_provider(mock_caller, "claude-sonnet-4-6")
    result = provider(symptom="worker down", context="")
    assert isinstance(result, list)
    mock_caller.assert_called_once()


# ── _parse_plan_response ──────────────────────────────────────────────────────


def test_parse_plan_response_valid_json_array() -> None:
    text = '[{"plugin_id": "restart", "params": {}, "description": "restart"}]'
    result = _parse_plan_response(text)
    assert len(result) == 1
    assert result[0]["plugin_id"] == "restart"


def test_parse_plan_response_markdown_fenced() -> None:
    text = '```json\n[{"plugin_id": "df", "params": {}, "description": "check disk"}]\n```'
    result = _parse_plan_response(text)
    assert result[0]["plugin_id"] == "df"


def test_parse_plan_response_invalid_returns_empty() -> None:
    result = _parse_plan_response("not a list at all")
    assert result == []


def test_parse_plan_response_json_object_returns_empty() -> None:
    # A JSON object (not array) should return []
    result = _parse_plan_response('{"plugin_id": "x"}')
    assert result == []
