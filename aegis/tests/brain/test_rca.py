"""Tests for aegis.server.brain.rca — S1-RCA assembly + gate logic."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aegis.server.brain.rca import (
    _check_rca_budget,
    _make_react_llm_provider,
    _parse_react_response,
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


# ── _check_rca_budget (per-org daily dollar gate, from llm_cost_ledger) ────────


def _pool_yielding() -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


@pytest.mark.asyncio
async def test_check_rca_budget_disabled_returns_true() -> None:
    """Daily gate off → always allowed (ledger never queried)."""
    assert await _check_rca_budget("org", _cfg(rca_max_cost_usd_per_org_daily=0)) is True


@pytest.mark.asyncio
async def test_check_rca_budget_blocks_when_spend_reaches_cap() -> None:
    cfg = _cfg()  # daily cap 25.0
    with (
        patch("aegis.server.persistence.get_pool", return_value=_pool_yielding()),
        patch(
            "aegis.server.services.llm_cost.org_spend",
            new_callable=AsyncMock,
            return_value={"total_usd": 10.0},
        ),
    ):
        assert await _check_rca_budget("org", cfg) is True  # under cap
    with (
        patch("aegis.server.persistence.get_pool", return_value=_pool_yielding()),
        patch(
            "aegis.server.services.llm_cost.org_spend",
            new_callable=AsyncMock,
            return_value={"total_usd": 25.0},
        ),
    ):
        assert await _check_rca_budget("org", cfg) is False  # at/over cap


@pytest.mark.asyncio
async def test_check_rca_budget_fail_open_configurable_on_db_error() -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ConnectionError("db down")

    # default fail_open=True → allow despite the error
    with patch("aegis.server.persistence.get_pool", side_effect=_boom):
        assert await _check_rca_budget("org", _cfg()) is True
    # fail_open=False → block on error (cost-safety mode)
    with patch("aegis.server.persistence.get_pool", side_effect=_boom):
        assert await _check_rca_budget("org", _cfg(rca_budget_fail_open=False)) is False


def test_build_knowledge_retrieval_fn_always_callable() -> None:
    """检索恒可用（无 embedder 时走 pg_trgm 词法保底），返回可调用的 retrieve fn。"""
    from aegis.server.brain.rca import _build_knowledge_retrieval_fn

    fn = _build_knowledge_retrieval_fn(_cfg(embedding_provider="fts"))
    assert callable(fn)


def test_knowledge_retrieval_formats_runbook_results() -> None:
    """检索到的 runbook 被格式化成 LLM 可读文本喂进 context。"""
    from aegis.server.brain import rca
    from aegis.server.services import runbook_store

    runbook_store._INDEX = [
        {
            "name": "restart-nginx",
            "title": "restart-nginx",
            "content": "restart the nginx container when it is unhealthy",
            "tags": ["container_unhealthy"],
            "embedding": None,
        }
    ]
    fn = rca._build_knowledge_retrieval_fn(_cfg(embedding_provider="fts", runbook_min_score=0.0))
    out = fn("nginx unhealthy container")
    assert "restart-nginx" in out and "Relevant runbooks" in out
