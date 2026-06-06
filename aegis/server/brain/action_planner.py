"""Action Planner — ActionPlannerEngine assembly for Aegis platform.

S4: ActionPlannerEngine uses real plugin_registry via importlib.metadata entry_points.
    Plugin discovery: aegis.server.plugins.registry.get_plugin_callable.

Interface gap requiring wrapper:
- llm_provider protocol: ActionPlannerEngine calls llm_provider(symptom=, context=)
  → [{plugin_id, params, description}] but ProviderRegistry callers have a different
  signature. _make_planner_llm_provider bridges this.
- rag: oskill.retrieve_runbook requires vector_encode_fn + vector_search_fn; deferred.

TODO(AEGIS-BACKLOG-070): switch to assemble(manifest) after oservice v0.4.2 extends
  _detect_element_kind to accept kind='layer4'.
TODO(AEGIS-BACKLOG-074): ActionPlannerEngine exposes only queue-based API; _execute_plan
  is called directly here for FastAPI request/response pattern.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from obase import ProviderRegistry
from oservice.engines.action_planner import ActionPlannerEngine

from aegis.server.plugins.registry import get_plugin_callable
from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)

_PLANNER_SYSTEM_PROMPT = (
    "You are an Aegis action planner. Given a symptom and optional context, "
    "generate a concrete remediation plan as a JSON array of steps. "
    "Each step must have: plugin_id (str), params (object), description (str). "
    "Respond ONLY with a valid JSON array, e.g.: "
    '[{{"plugin_id": "restart_service", "params": {{"name": "worker"}}, '
    '"description": "restart the worker process"}}]'
)


# ── LLM provider wrapper ──────────────────────────────────────────────────────
# ActionPlannerEngine calls: llm_provider(symptom=str, context=str) → list[dict]
# ProviderRegistry callers accept: (messages=list, model=str, max_tokens=int, ...)


def _extract_text(resp: dict[str, Any]) -> str:
    content = resp.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return str(resp)


def _parse_plan_response(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    for fence in ("```json", "```"):
        if fence in text and text.count("```") >= 2:
            inner = text.split(fence, 1)[1].split("```", 1)[0].strip()
            try:
                result = json.loads(inner)
                if isinstance(result, list):
                    return result  # type: ignore[return-value]
            except json.JSONDecodeError:
                pass
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result  # type: ignore[return-value]
    except json.JSONDecodeError:
        pass
    log.warning("planner_llm_parse_failed: response not a JSON list, returning empty plan")
    return []


def _make_planner_llm_provider(
    base_caller: Callable[..., Any] | None,
    model: str,
) -> Callable[..., Any]:
    """Return an llm_provider callable compatible with ActionPlannerEngine protocol."""

    def _provider(*, symptom: str, context: str) -> list[dict[str, Any]]:
        if base_caller is None:
            return []
        messages = [
            {
                "role": "user",
                "content": (
                    f"Symptom: {symptom}\n\nContext: {context}"
                    "\n\nGenerate remediation steps (JSON array):"
                ),
            }
        ]
        try:
            resp = base_caller(
                messages=messages,
                model=model,
                max_tokens=1024,
                system=_PLANNER_SYSTEM_PROMPT,
            )
            return _parse_plan_response(_extract_text(resp))
        except Exception as exc:
            log.warning("planner_llm_call_failed: %s", exc)
            return []

    return _provider


# ── Assembly ──────────────────────────────────────────────────────────────────


def build_planner_service(cfg: AegisSettings) -> ActionPlannerEngine:
    if ProviderRegistry.has("llm", cfg.llm_provider):
        raw_caller: Callable[..., Any] | None = ProviderRegistry.get("llm", cfg.llm_provider)
    else:
        log.warning(
            "planner_build: llm provider %r not registered — planner will return empty plans",
            cfg.llm_provider,
        )
        raw_caller = None

    llm_provider = _make_planner_llm_provider(raw_caller, cfg.planner_llm_model)

    # Direct instantiation (AEGIS-BACKLOG-070)
    return ActionPlannerEngine(
        llm_provider=llm_provider,
        plugin_registry=get_plugin_callable,
        rag=None,  # TODO(AEGIS-BACKLOG-073): wire retrieve_runbook wrapper
        trigger={},
        config={"max_retries": 1, "step_timeout_seconds": 30},
        name="aegis-action-planner",
    )


# ── Module-level singleton ────────────────────────────────────────────────────

_planner_service: ActionPlannerEngine | None = None


def get_planner_service() -> ActionPlannerEngine | None:
    return _planner_service


def init_planner_service(cfg: AegisSettings) -> ActionPlannerEngine:
    global _planner_service
    _planner_service = build_planner_service(cfg)
    return _planner_service


# ── Brain API ─────────────────────────────────────────────────────────────────


async def propose_action_plan(
    investigation_result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Propose action plan based on RCA investigation result.

    Returns [{plugin_id, params, description}, ...], or [] if service not ready.
    Direct call bypasses queue — TODO(AEGIS-BACKLOG-074).
    """
    service = get_planner_service()
    if service is None:
        log.error("planner_service_not_initialized — call init_planner_service first")
        return []

    request = {
        "symptom": investigation_result.get("final_answer", ""),
        "context": investigation_result.get("history", []),
    }
    result = await service._execute_plan(request)  # type: ignore[attr-defined]
    return result.get("step_results", [])
