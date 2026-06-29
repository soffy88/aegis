"""Action Planner — ActionPlannerEngine assembly for Aegis platform.

S4: ActionPlannerEngine uses real plugin_registry via importlib.metadata entry_points.
    Plugin discovery: aegis.server.plugins.registry.get_plugin_callable.

Interface gap requiring wrapper:
- llm_provider protocol: ActionPlannerEngine calls llm_provider(symptom=, context=)
  → [{plugin_id, params, description}] but ProviderRegistry callers have a different
  signature. _make_planner_llm_provider bridges this.
- rag: reuses the RCA runbook-retrieval builder (vector_encode_fn + vector_search_fn
  over LanceDB) so the planner grounds plans in matching runbooks when the vector
  store is initialized (AEGIS-BACKLOG-073 resolved).

AEGIS-BACKLOG-070: switched to assemble(manifest).
AEGIS-BACKLOG-074: using public async invoke API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from obase import ProviderRegistry
from oservice.assembler import ServiceManifest, assemble
from oservice.engines.action_planner import ActionPlannerEngine

from aegis.server.brain.rca import _build_knowledge_retrieval_fn
from aegis.server.plugins.registry import get_plugin_callable
from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)

_PLANNER_SYSTEM_PROMPT = (
    "You are an Aegis action planner. Given a symptom and optional context, "
    "generate a concrete remediation plan as a JSON array of steps. "
    "Each step must have: plugin_id (str), params (object), description (str). "
    "Respond ONLY with a valid JSON array, e.g.: "
    '[{"plugin_id": "restart_planner_service", "params": {"name": "worker"}, '
    '"description": "restart the worker process"}]'
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
            return {"steps": []}

    _provider.__module__ = "obase.aegis_bridge"
    return _provider


# ── Assembly ──────────────────────────────────────────────────────────────────


def build_planner_service(cfg: AegisSettings) -> ActionPlannerEngine:
    if ProviderRegistry.has("llm", cfg.llm_provider):
        raw_caller: Callable[..., Any] | None = ProviderRegistry.get("llm", cfg.llm_provider)
    else:
        log.warning(
            "planner_build: llm provider %r not registered — planning will use stub",
            cfg.llm_provider,
        )
        raw_caller = None

    llm_provider = _make_planner_llm_provider(raw_caller, cfg.planner_llm_model)
    get_plugin_callable.__module__ = "layer4.aegis_bridge"

    # Ground plans in matching runbooks when the vector store is ready; empty otherwise.
    retrieve_fn = _build_knowledge_retrieval_fn(cfg)
    rag = [retrieve_fn] if retrieve_fn is not None else []

    manifest = ServiceManifest(
        skeleton="action_planner",
        inject={
            "llm_provider": [llm_provider],
            "plugin_registry": [get_plugin_callable],
            "rag": rag,
        },
        trigger={},
        config={
            "max_retries": 2,
            "step_timeout_seconds": 30,
        },
        name="aegis-action-planner",
    )
    return assemble(manifest)  # type: ignore[return-value]


_planner_service: ActionPlannerEngine | None = None


def init_planner_service(cfg: AegisSettings) -> ActionPlannerEngine:
    global _planner_service
    _planner_service = build_planner_service(cfg)
    return _planner_service


def get_planner_service() -> ActionPlannerEngine | None:
    return _planner_service


async def propose_action_plan(
    symptom: str, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    service = get_planner_service()
    if service is None:
        return []

    request = {"symptom": symptom, "evidence": context or {}}
    return await service.invoke(request)
