"""Triage — TriageEngine assembly for Aegis platform.

AEGIS-BACKLOG-070: switched to assemble(manifest).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from obase import ProviderRegistry
from oservice.assembler import ServiceManifest, assemble
from oservice.engines.triage import TriageEngine

from aegis.server.runtime.config import AegisSettings

log = logging.getLogger(__name__)

_TRIAGE_SYSTEM_PROMPT = (
    "You are an AIOps triage classifier. Given a list of events, assign each a "
    "priority_score (0-100). Higher score = higher urgency. "
    "Return ONLY a JSON array with each original event's fields preserved plus "
    "priority_score (int 0-100), classified_category (str), "
    "should_escalate (bool), and reason (str). No markdown fences."
)


def _build_triage_messages(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    payload = json.dumps(events, default=str)
    return [{"role": "user", "content": f"Triage these events:\n{payload}"}]


def _extract_text(resp: dict[str, Any]) -> str:
    for block in resp.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            return block["text"]
    return ""


def _parse_triage_response(text: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse LLM response into list[dict] with priority_score. Falls back to raw events."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: return events with default priority_score=50
    return [
        {"priority_score": 50, "should_escalate": True, "reason": "parse_error", **e}
        for e in events
    ]


def _make_triage_llm_caller(
    base_caller: Callable[..., Any] | None, model: str, max_tokens: int
) -> Callable[..., Any]:
    """Bridge ProviderRegistry caller → TriageEngine llm_caller protocol.

    TriageEngine calls: llm_caller(config=dict, events=list, mode="score")
    ProviderRegistry caller: (messages=list, model=str, max_tokens=int, system=str, ...) → dict
    """

    def _llm_caller(
        *, config: dict[str, Any], events: list[dict[str, Any]], mode: str = "score"
    ) -> list[dict[str, Any]]:
        if base_caller is None:
            log.warning(
                "triage_llm_not_configured provider_not_registered; fallback priority_score=50"
            )
            return [
                {"priority_score": 50, "should_escalate": True, "reason": "llm_not_configured", **e}
                for e in events
            ]

        effective_model = config.get("model", model)
        effective_max_tokens = int(config.get("max_tokens", max_tokens))

        try:
            resp = base_caller(
                messages=_build_triage_messages(events),
                model=effective_model,
                max_tokens=effective_max_tokens,
                system=_TRIAGE_SYSTEM_PROMPT,
            )
            return _parse_triage_response(_extract_text(resp), events)
        except Exception as exc:  # noqa: BLE001
            log.warning("triage_llm_caller_error: %s; fallback priority_score=50", exc)
            return [
                {"priority_score": 50, "should_escalate": True, "reason": f"llm_error:{exc}", **e}
                for e in events
            ]

    _llm_caller.__module__ = "obase.aegis_bridge"
    return _llm_caller


# ── Assembly ──────────────────────────────────────────────


def build_triage_service(cfg: AegisSettings) -> TriageEngine:
    """Assemble TriageEngine with on_signal trigger and LLM bridge."""
    has_provider = ProviderRegistry.has("llm", cfg.llm_provider)
    raw_caller: Callable[..., Any] | None = (
        ProviderRegistry.get("llm", cfg.llm_provider) if has_provider else None
    )

    llm_caller = _make_triage_llm_caller(raw_caller, cfg.triage_llm_model, cfg.triage_max_tokens)

    manifest = ServiceManifest(
        skeleton="triage",
        inject={
            "llm_caller": [llm_caller],
            "filters": [],
        },
        trigger={"on_signal": True},
        config={
            "llm_config": {
                "model": cfg.triage_llm_model,
                "max_tokens": cfg.triage_max_tokens,
            },
        },
        name="aegis-triage",
    )
    svc = assemble(manifest)
    svc.run()
    return svc


_triage_service: TriageEngine | None = None


def get_triage_service() -> TriageEngine | None:
    return _triage_service


def init_triage_service(cfg: AegisSettings) -> TriageEngine:
    global _triage_service
    _triage_service = build_triage_service(cfg)
    return _triage_service


async def triage_signal(signal: dict[str, Any]) -> dict[str, Any]:
    """Triage a signal via TriageEngine.process(). Returns scored signal dict or fallback."""
    svc = get_triage_service()
    if svc is None:
        log.warning("triage_service_not_initialized signal_id=%s", signal.get("signal_id"))
        return {
            "should_escalate": True,
            "severity": signal.get("severity", "medium"),
            "priority_score": 50,
            "reason": "triage_service_not_initialized",
            "classified_category": None,
        }

    result = await svc.process(signal)
    if result is None:
        # process() returns None when all filters reject the signal
        return {
            "should_escalate": False,
            "severity": signal.get("severity", "medium"),
            "priority_score": 0,
            "reason": "filtered_out",
            "classified_category": None,
        }
    return result
