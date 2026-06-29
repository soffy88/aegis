"""RCA Agent — AgenticLoopEngine assembly for Aegis platform.

Now uses assemble(manifest) pattern (BACKLOG-070 resolved).
Uses public async invoke API (BACKLOG-074 resolved).

Interface gaps requiring wrappers:
- llm_provider protocol: AgenticLoopEngine calls llm_provider(task=, context=, history=)
  but ProviderRegistry callers have signature (messages=, model=, max_tokens=, ...).
  _make_react_llm_provider bridges this gap.
- knowledge_retrieval: oskill.retrieve_runbook requires vector_encode_fn + vector_search_fn;
  Uses RAG-based knowledge retrieval via oskill.retrieve_runbook (AEGIS-BACKLOG-073 resolved).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from obase import ProviderRegistry
from oprim import (
    docker_inspect,
    docker_logs,
    docker_stats,
    fs_disk_usage,
    network_http_health,
    network_port_check,
    postgres_locks,
    postgres_long_running_queries,
    postgres_pool_status,
    rabbitmq_consumer_count,
    rabbitmq_queue_depth,
    system_cpu_usage,
    system_load_avg,
    system_ram_usage,
)
from oservice.assembler import ServiceManifest, assemble
from oservice.engines.agentic_loop import AgenticLoopEngine
from oskill import retrieve_runbook

from aegis.server.runtime.config import AegisSettings
from aegis.server.services.vector_store import (
    get_vector_db,
    make_vector_encode_fn,
    make_vector_search_fn,
)

log = logging.getLogger(__name__)

# 14 oprim RCA tools (advisor SPEC §1.2)
_RCA_TOOLS: list[Callable[..., Any]] = [
    postgres_pool_status,
    postgres_long_running_queries,
    postgres_locks,
    rabbitmq_queue_depth,
    rabbitmq_consumer_count,
    docker_logs,
    docker_inspect,
    docker_stats,
    network_http_health,
    network_port_check,
    system_cpu_usage,
    system_ram_usage,
    system_load_avg,
    fs_disk_usage,
]

_REACT_SYSTEM_PROMPT = (
    "You are an Aegis RCA agent performing root cause analysis. "
    "Use the available tools to investigate the problem step by step. "
    "Respond ONLY with valid JSON in one of two formats:\n"
    "  Tool call: "
    '{{"thought": "...", "action": "use_tool", "tool_name": "...", "tool_args": {{...}}}}\n'
    "  Final answer: "
    '{{"final_answer": "root cause: ..."}}'
)


# ── LLM provider wrapper ──────────────────────────────────────────────────────
# AgenticLoopEngine calls: llm_provider(task=dict, context=str, history=list) → dict
# ProviderRegistry callers accept: (messages=list, model=str, max_tokens=int, ...)
# Bridge: format ReAct prompt → call provider → parse JSON ReAct response.


def _build_react_messages(
    task: dict[str, Any], context: str, history: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    parts = [f"Task: {json.dumps(task, ensure_ascii=False)}"]
    if context:
        parts.append(f"Context: {context}")
    if history:
        parts.append(f"Previous steps: {json.dumps(history, ensure_ascii=False)}")
    parts.append("What is your next action? Respond with JSON only.")
    return [
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def _extract_text(resp: dict[str, Any]) -> str:
    content = resp.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return str(resp)


def _parse_react_response(text: str) -> dict[str, Any]:
    text = text.strip()
    for fence in ("```json", "```"):
        if fence in text and text.count("```") >= 2:
            inner = text.split(fence, 1)[1].split("```", 1)[0].strip()
            try:
                return json.loads(inner)  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                pass
    try:
        return json.loads(text)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        return {"final_answer": text}


def _make_react_llm_provider(
    base_caller: Callable[..., Any] | None,
    model: str,
) -> Callable[..., Any]:
    """Return an llm_provider callable compatible with AgenticLoopEngine protocol."""

    def _provider(
        *, task: dict[str, Any], context: str, history: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if base_caller is None:
            return {"final_answer": "RCA LLM not configured (provider not registered)"}
        messages = _build_react_messages(task, context, history)
        try:
            resp = base_caller(
                messages=messages,
                model=model,
                max_tokens=2048,
                system=_REACT_SYSTEM_PROMPT,
            )
            return _parse_react_response(_extract_text(resp))
        except Exception as exc:
            log.warning("rca_llm_call_failed: %s", exc)
            return {"final_answer": f"LLM error: {exc}"}

    _provider.__module__ = "obase.aegis_bridge"
    return _provider


# ── Assembly ──────────────────────────────────────────────────────────────────


def _build_knowledge_retrieval_fn(cfg: AegisSettings) -> Callable[[str], Any]:
    """构建 knowledge_retrieval callable，注入 AgenticLoopEngine."""
    db = get_vector_db()
    if db is None:
        log.warning("rca: vector_db not initialized, knowledge_retrieval disabled")
        return None

    encode_fn = make_vector_encode_fn(provider=cfg.embedding_provider)
    search_fn = make_vector_search_fn(db=db, collection=cfg.runbook_vector_collection)

    def _retrieve(query: str) -> str:
        try:
            result = retrieve_runbook(
                query=query,
                vector_encode_fn=encode_fn,
                vector_search_fn=search_fn,
                top_k=cfg.runbook_top_k,
                min_score=cfg.runbook_min_score,
                collection=cfg.runbook_vector_collection,
            )
            if not result.results:
                return ""
            # 序列化为 LLM 可读的文本
            parts = [f"Relevant runbooks for '{query}':"]
            for entry in result.results:
                parts.append(f"\n## {entry.title} (score={entry.score:.2f})\n{entry.content}")
            return "\n".join(parts)
        except Exception as exc:
            log.warning("rca_knowledge_retrieval_failed: %s", exc)
            return ""

    _retrieve.__module__ = "oskill.aegis_bridge"
    return _retrieve


def build_rca_service(cfg: AegisSettings) -> AgenticLoopEngine:
    if ProviderRegistry.has("llm", cfg.llm_provider):
        raw_caller: Callable[..., Any] | None = ProviderRegistry.get("llm", cfg.llm_provider)
    else:
        log.warning(
            "rca_build: llm provider %r not registered — investigation will use stub",
            cfg.llm_provider,
        )
        raw_caller = None

    llm_provider = _make_react_llm_provider(raw_caller, cfg.rca_llm_model)
    knowledge_retrieval = _build_knowledge_retrieval_fn(cfg)

    manifest = ServiceManifest(
        skeleton="agentic_loop",
        inject={
            "llm_provider": [llm_provider],
            "tools": _RCA_TOOLS,
            "knowledge_retrieval": [knowledge_retrieval],
        },
        trigger={},
        config={"max_steps": cfg.rca_max_steps},
        name="aegis-rca",
    )
    return assemble(manifest)  # type: ignore[return-value]


# ── Module-level singleton ────────────────────────────────────────────────────

_rca_service: AgenticLoopEngine | None = None


def get_rca_service() -> AgenticLoopEngine | None:
    return _rca_service


def init_rca_service(cfg: AegisSettings) -> AgenticLoopEngine:
    global _rca_service
    _rca_service = build_rca_service(cfg)
    return _rca_service


# ── Brain decision function ───────────────────────────────────────────────────


async def investigate_if_deep_needed(
    diagnose_result: dict[str, Any],
    cfg: AegisSettings,
    org_id: str | None = None,
) -> dict[str, Any] | None:
    """Gate function: only launch agentic RCA when warranted.

    Early exits (in order):
    1. needs_deep_investigation=False → None
    2. severity not in ('critical', 'high') → None (save Sonnet tokens)
    3. per-org budget exceeded → None
    4. service not initialized → None
    5. passes all → call _execute_task directly (AEGIS-BACKLOG-074)
    """
    if not diagnose_result.get("needs_deep_investigation"):
        return None

    severity = diagnose_result.get("severity")
    if severity not in ("critical", "high"):
        log.info("rca_skip_severity severity=%s not in (critical, high)", severity)
        return None

    if org_id and not await _check_rca_budget(org_id, cfg):
        log.warning("rca_skip_budget org_id=%s daily budget exceeded", org_id)
        return None

    service = get_rca_service()
    if service is None:
        log.error("rca_service_not_initialized — call init_rca_service first")
        return None

    task = {
        "goal": diagnose_result.get("triage_summary", ""),
        "context": diagnose_result.get("evidence", {}),
    }
    # Direct call bypasses queue — needed for request/response in FastAPI context.
    # TODO(AEGIS-BACKLOG-074): remove when oservice exposes public async invoke API.
    return await service.invoke(task)


def _rca_daily_max_invocations(cfg: AegisSettings) -> int | None:
    """Max deep investigations per org per day, or None if the daily gate is off.

    Each investigation reserves one per-invocation slot, so the daily ceiling is
    daily_budget / per_invocation_cap (at least 1 when both are positive).
    """
    per_inv = cfg.rca_max_cost_usd_per_invocation
    daily = cfg.rca_max_cost_usd_per_org_daily
    if per_inv <= 0 or daily <= 0:
        return None
    return max(1, int(daily // per_inv))


async def _check_rca_budget(org_id: str, cfg: AegisSettings) -> bool:
    """Per-org daily RCA budget gate (Redis-shared across workers).

    Reserves one slot per call via an atomic INCR on a per-org per-day key. Returns
    False once the day's investigation count exceeds the derived ceiling. Fails OPEN
    on any Redis error or when the gate is disabled — incident response must never be
    blocked by a budget-infra outage; the per-invocation cap (OmodulDispatcher) still
    bounds single-call cost.
    """
    daily_max = _rca_daily_max_invocations(cfg)
    if daily_max is None:
        log.warning(
            "rca_budget: daily gate disabled (per_invocation=%.2f daily=%.2f) — "
            "set positive values to cap per-org daily spend",
            cfg.rca_max_cost_usd_per_invocation,
            cfg.rca_max_cost_usd_per_org_daily,
        )
        return True

    day = datetime.now(UTC).strftime("%Y%m%d")
    key = f"aegis:rca_budget:{org_id}:{day}"
    client = None
    try:
        client = aioredis.from_url(cfg.redis_url)
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 2 * 86400)
        if count > daily_max:
            log.warning(
                "rca_budget_exceeded org_id=%s count=%d daily_max=%d",
                org_id,
                count,
                daily_max,
            )
            return False
        log.debug("rca_budget org_id=%s count=%d/%d", org_id, count, daily_max)
        return True
    except Exception as exc:  # noqa: BLE001 — fail open, never block RCA on infra
        log.warning("rca_budget_redis_failed org_id=%s err=%s — allowing", org_id, exc)
        return True
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
