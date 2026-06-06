"""RCA Agent — AgenticLoopEngine assembly for Aegis platform.

S1 bypass: AgenticLoopEngine is instantiated directly (not via assemble()) because
oservice v0.4.1 _detect_element_kind rejects aegis.* __module__ callables.

Interface gaps requiring wrappers (analogous to AEGIS-BACKLOG-071 in alerter):
- llm_provider protocol: AgenticLoopEngine calls llm_provider(task=, context=, history=)
  but ProviderRegistry callers have signature (messages=, model=, max_tokens=, ...).
  _make_react_llm_provider bridges this gap.
- knowledge_retrieval: oskill.retrieve_runbook requires vector_encode_fn + vector_search_fn;
  not wirable in S1 — deferred to AEGIS-BACKLOG-073.

TODO(AEGIS-BACKLOG-070): bypass new AgenticLoopEngine; switch to assemble(manifest) after
  oservice v0.4.2 fixes _detect_element_kind to accept layer4/aegis module prefixes.
TODO(AEGIS-BACKLOG-073): wire oskill.retrieve_runbook via knowledge_retrieval wrapper
  once vector store + encode fn are configured (S2).
TODO(AEGIS-BACKLOG-074): AgenticLoopEngine exposes only queue-based API (submit_task +
  on_task_done callback); _execute_task is called directly here for FastAPI request/response.
  oservice v0.4.2 should expose a public async invocation API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

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
from oservice.engines.agentic_loop import AgenticLoopEngine

from aegis.server.runtime.config import AegisSettings

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

    return _provider


# ── Assembly ──────────────────────────────────────────────────────────────────


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

    # Direct instantiation (AEGIS-BACKLOG-070)
    return AgenticLoopEngine(
        llm_provider=llm_provider,
        tools=_RCA_TOOLS,
        knowledge_retrieval=None,  # TODO(AEGIS-BACKLOG-073): wire retrieve_runbook wrapper
        trigger={},
        config={"max_steps": cfg.rca_max_steps},
        name="aegis-rca",
    )


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

    if org_id and not _check_rca_budget(org_id, cfg):
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
    return await service._execute_task(task)  # type: ignore[attr-defined]


def _check_rca_budget(org_id: str, cfg: AegisSettings) -> bool:
    """Per-org per-day RCA budget gate.

    TODO(AEGIS-BACKLOG-072): wire obase.CostTracker for real per-org daily accounting.
    Stub returns True (no limit) — safe for M1 self-hosted where org_id is always own org.
    """
    _ = org_id, cfg  # noqa: F841
    return True
