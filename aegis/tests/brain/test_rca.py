"""Tests for aegis.server.brain.rca — S1-RCA assembly + gate logic."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.brain.rca import (
    _check_rca_budget,
    _make_react_llm_provider,
    _parse_react_response,
    _rca_daily_max_invocations,
    build_rca_service,
    get_rca_service,
    init_rca_service,
    investigate_if_deep_needed,
)
from aegis.server.runtime.config import AegisSettings


def _cfg(**kwargs: Any) -> AegisSettings:
    return AegisSettings(**kwargs)  # type: ignore[arg-type]


# ── build / singleton ─────────────────────────────────────────────────────────


def test_build_rca_service_returns_engine() -> None:
    svc = build_rca_service(_cfg())
    assert hasattr(svc, "run")
    assert hasattr(svc, "health")
    assert hasattr(svc, "submit_task")


def test_init_rca_service_sets_singleton() -> None:
    svc = init_rca_service(_cfg())
    assert get_rca_service() is svc


# ── investigate_if_deep_needed: early exits ───────────────────────────────────


@pytest.mark.asyncio
async def test_investigate_skip_when_flag_false() -> None:
    result = await investigate_if_deep_needed(
        {"needs_deep_investigation": False, "severity": "critical"},
        _cfg(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_investigate_skip_low_severity() -> None:
    result = await investigate_if_deep_needed(
        {"needs_deep_investigation": True, "severity": "low"},
        _cfg(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_investigate_skip_medium_severity() -> None:
    result = await investigate_if_deep_needed(
        {"needs_deep_investigation": True, "severity": "medium"},
        _cfg(),
    )
    assert result is None


@pytest.mark.asyncio
async def test_investigate_skip_budget_exceeded() -> None:
    with patch(
        "aegis.server.brain.rca._check_rca_budget", new_callable=AsyncMock, return_value=False
    ):
        result = await investigate_if_deep_needed(
            {"needs_deep_investigation": True, "severity": "critical"},
            _cfg(),
            org_id="org_123",
        )
    assert result is None


@pytest.mark.asyncio
async def test_investigate_skip_service_not_initialized() -> None:
    with patch("aegis.server.brain.rca._rca_service", None):
        result = await investigate_if_deep_needed(
            {"needs_deep_investigation": True, "severity": "critical"},
            _cfg(),
        )
    assert result is None


@pytest.mark.asyncio
async def test_investigate_calls_execute_task_on_critical() -> None:
    mock_engine = MagicMock()
    mock_engine.invoke = AsyncMock(
        return_value={"status": "completed", "final_answer": "disk full"}
    )
    with patch("aegis.server.brain.rca._rca_service", mock_engine):
        result = await investigate_if_deep_needed(
            {
                "needs_deep_investigation": True,
                "severity": "critical",
                "triage_summary": "disk usage 97%",
                "evidence": {"path": "/"},
            },
            _cfg(),
        )
    mock_engine.invoke.assert_awaited_once()
    task_arg = mock_engine.invoke.call_args[0][0]
    assert task_arg["goal"] == "disk usage 97%"
    assert result == {"status": "completed", "final_answer": "disk full"}


@pytest.mark.asyncio
async def test_investigate_calls_execute_task_on_high() -> None:
    mock_engine = MagicMock()
    mock_engine.invoke = AsyncMock(return_value={"status": "completed", "final_answer": "oom"})
    with patch("aegis.server.brain.rca._rca_service", mock_engine):
        result = await investigate_if_deep_needed(
            {"needs_deep_investigation": True, "severity": "high", "triage_summary": "oom"},
            _cfg(),
        )
    assert result is not None
    assert result["status"] == "completed"


# ── _make_react_llm_provider ──────────────────────────────────────────────────


def test_react_llm_provider_returns_final_answer_when_no_caller() -> None:
    provider = _make_react_llm_provider(None, "any-model")
    result = provider(task={"goal": "x"}, context="", history=[])
    assert "final_answer" in result


def test_react_llm_provider_calls_base_and_parses_json() -> None:
    mock_caller = MagicMock(
        return_value={"content": [{"type": "text", "text": '{"final_answer": "disk full"}'}]}
    )
    provider = _make_react_llm_provider(mock_caller, "claude-sonnet-4-6")
    result = provider(task={"goal": "check disk"}, context="", history=[])
    assert result == {"final_answer": "disk full"}
    mock_caller.assert_called_once()


# ── _parse_react_response ─────────────────────────────────────────────────────


def test_parse_react_response_plain_json() -> None:
    result = _parse_react_response('{"final_answer": "all good"}')
    assert result["final_answer"] == "all good"


def test_parse_react_response_markdown_fenced() -> None:
    text = '```json\n{"tool_name": "df", "tool_args": {}}\n```'
    result = _parse_react_response(text)
    assert result["tool_name"] == "df"


def test_parse_react_response_fallback_to_text() -> None:
    result = _parse_react_response("not json at all")
    assert result["final_answer"] == "not json at all"


# ── _check_rca_budget (per-org daily Redis gate) ──────────────────────────────


def test_rca_daily_max_invocations_derivation() -> None:
    assert _rca_daily_max_invocations(_cfg()) == 5  # 25 / 5 default
    assert (
        _rca_daily_max_invocations(_cfg(rca_max_cost_usd_per_org_daily=0)) is None
    )  # disabled
    assert (
        _rca_daily_max_invocations(_cfg(rca_max_cost_usd_per_invocation=0)) is None
    )  # disabled
    # ceiling is at least 1 even when daily < per-invocation
    assert _rca_daily_max_invocations(_cfg(rca_max_cost_usd_per_org_daily=1)) == 1


@pytest.mark.asyncio
async def test_check_rca_budget_disabled_returns_true() -> None:
    """Daily gate off → always allowed (Redis never touched)."""
    assert await _check_rca_budget("org", _cfg(rca_max_cost_usd_per_org_daily=0)) is True


@pytest.mark.asyncio
async def test_check_rca_budget_allows_under_ceiling_then_blocks() -> None:
    counter = {"n": 0}

    class _FakeRedis:
        async def incr(self, key: str) -> int:
            counter["n"] += 1
            return counter["n"]

        async def expire(self, key: str, ttl: int) -> None:
            pass

        async def aclose(self) -> None:
            pass

    with patch("aegis.server.brain.rca.aioredis.from_url", return_value=_FakeRedis()):
        cfg = _cfg()  # daily_max = 5
        results = [await _check_rca_budget("org", cfg) for _ in range(6)]
    assert results[:5] == [True, True, True, True, True]
    assert results[5] is False  # 6th call exceeds the ceiling


@pytest.mark.asyncio
async def test_check_rca_budget_fails_open_on_redis_error() -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ConnectionError("redis down")

    with patch("aegis.server.brain.rca.aioredis.from_url", side_effect=_boom):
        assert await _check_rca_budget("org", _cfg()) is True


def test_build_knowledge_retrieval_fn_returns_none_when_no_db() -> None:
    """vector_db 未初始化时 _build_knowledge_retrieval_fn 返回 None."""
    from aegis.server.brain.rca import _build_knowledge_retrieval_fn

    with patch("aegis.server.brain.rca.get_vector_db", return_value=None):
        fn = _build_knowledge_retrieval_fn(_cfg())
    assert fn is None


def test_build_knowledge_retrieval_fn_returns_callable_when_db_ready() -> None:
    """vector_db 就绪时返回可调用的 retrieve fn."""
    from aegis.server.brain.rca import _build_knowledge_retrieval_fn

    mock_db = MagicMock()
    with patch("aegis.server.brain.rca.get_vector_db", return_value=mock_db):
        fn = _build_knowledge_retrieval_fn(_cfg())
    assert callable(fn)
