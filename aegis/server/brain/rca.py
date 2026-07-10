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
from typing import Any

from obase import ProviderRegistry
from obase.docker import docker_inspect, docker_logs, docker_stats
from oprim import disk_usage as fs_disk_usage
from oprim import postgres_locks_status as postgres_locks
from oprim import (
    postgres_pool_status,
    rabbitmq_consumer_count,
    rabbitmq_queue_depth,
    system_cpu_usage,
    system_load_avg,
    system_ram_usage,
)
from oprim import postgres_slow_queries as postgres_long_running_queries
from oprim._network import network_http_health, network_port_check  # v3 not top-level
from oservi.assembler import ServiceManifest, assemble
from oservi.engines.agentic_loop import AgenticLoopEngine

from aegis.server.runtime.config import AegisSettings
from aegis.server.services import runbook_store
from aegis.server.services.embeddings import get_embedder

log = logging.getLogger(__name__)


def _as_oprim_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """oservi agentic_loop tools 注入要求 kind=oprim,但 docker 原语 v3 迁至 obase.docker
    (kind=obase)。用 bridge 包裹使 __module__ 判定为 oprim,行为透传不变。
    (更干净的修法是 oservi 放宽 tools kind 接受 obase.docker —— 属 3O 侧后续。)"""
    import functools

    @functools.wraps(fn)
    def _tool(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    _tool.__module__ = "oprim.aegis_bridge"
    return _tool


# 14 RCA tools (advisor SPEC §1.2); docker 三件套经 bridge 标记为 oprim kind
_RCA_TOOLS: list[Callable[..., Any]] = [
    postgres_pool_status,
    postgres_long_running_queries,
    postgres_locks,
    rabbitmq_queue_depth,
    rabbitmq_consumer_count,
    _as_oprim_tool(docker_logs),
    _as_oprim_tool(docker_inspect),
    _as_oprim_tool(docker_stats),
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

    _provider.__module__ = "oprim.aegis_bridge"  # oservi skeleton 要 llm kind=oprim
    return _provider


# ── Assembly ──────────────────────────────────────────────────────────────────


def _build_knowledge_retrieval_fn(cfg: AegisSettings) -> Callable[[str], Any]:
    """构建 knowledge_retrieval callable，注入 AgenticLoopEngine.

    走 runbook_store 的内存检索（语义 pgvector / 词法 pg_trgm 保底），同步、无 async
    DB 往返（agent 在 worker 线程里同步调用本工具）。无 runbook 时返回空串。
    """
    embedder = get_embedder(cfg)  # None → 词法保底

    def _retrieve(query: str) -> str:
        try:
            results = runbook_store.retrieve(
                query,
                top_k=cfg.runbook_top_k,
                min_score=cfg.runbook_min_score,
                embedder=embedder,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("rca_knowledge_retrieval_failed: %s", exc)
            return ""
        if not results:
            return ""
        parts = [f"Relevant runbooks for '{query}':"]
        for e in results:
            parts.append(f"\n## {e['title']} (score={e['score']:.2f})\n{e['content']}")
        return "\n".join(parts)

    _retrieve.__module__ = "oskill.aegis_bridge"
    return _retrieve


def build_rca_service(cfg: AegisSettings) -> AgenticLoopEngine:
    if ProviderRegistry.has("llm", cfg.llm_provider):
        raw_caller: Callable[..., Any] | None = ProviderRegistry.get().llm(cfg.llm_provider)
    else:
        log.warning(
            "rca_build: llm provider %r not registered — investigation will use stub",
            cfg.llm_provider,
        )
        raw_caller = None

    llm_provider = _make_react_llm_provider(raw_caller, cfg.rca_llm_model)
    knowledge_retrieval = _build_knowledge_retrieval_fn(cfg)

    # oservi v1.3.0 agentic_loop 契约变化:llm_provider→llm_caller、knowledge_retrieval→retrieval(0..1)、
    # 新增必需 turn_handler(omodul,预处理 prompt)。aegis 无预处理需求 → 注入 passthrough bridge。
    def _passthrough_turn_handler(*, messages: list[Any], context: Any = None) -> dict[str, Any]:
        return {"messages": messages}

    _passthrough_turn_handler.__module__ = "omodul.aegis_bridge"

    inject: dict[str, list[Any]] = {
        "llm_caller": [llm_provider],
        "tools": _RCA_TOOLS,
        "turn_handler": [_passthrough_turn_handler],
    }
    if knowledge_retrieval is not None:
        inject["retrieval"] = [knowledge_retrieval]  # 0..1:仅在 vector_db 就绪时注入

    manifest = ServiceManifest(
        skeleton="agentic_loop",
        inject=inject,
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


async def _check_rca_budget(org_id: str, cfg: AegisSettings) -> bool:
    """Per-org daily RCA budget gate, by actual USD spend from llm_cost_ledger.

    Returns False once the org's trailing 1-day LLM spend reaches the configured
    daily cap. This is dollar-accurate, replacing the prior invocation-count proxy
    (count = daily / per_invocation). On a DB/infra error the result is
    cfg.rca_budget_fail_open (default True — never block incident response on a
    budget-infra outage; the per-invocation cap still bounds single-call cost).
    """
    daily = cfg.rca_max_cost_usd_per_org_daily
    if daily <= 0:
        log.warning("rca_budget: daily gate disabled (rca_max_cost_usd_per_org_daily<=0)")
        return True

    try:
        from aegis.server.persistence import get_pool  # noqa: PLC0415
        from aegis.server.services.llm_cost import org_spend  # noqa: PLC0415

        async with get_pool().acquire() as conn:
            spend = await org_spend(conn, org_id=org_id, days=1.0)
        total = float(spend["total_usd"])
        if total >= daily:
            log.warning("rca_budget_exceeded org_id=%s spend=%.4f daily=%.2f", org_id, total, daily)
            return False
        log.debug("rca_budget org_id=%s spend=%.4f/%.2f", org_id, total, daily)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "rca_budget_check_failed org_id=%s err=%s — fail_open=%s",
            org_id,
            exc,
            cfg.rca_budget_fail_open,
        )
        return cfg.rca_budget_fail_open
